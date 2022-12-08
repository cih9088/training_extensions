"""Collection of utils for task implementation in Detection Task."""

# Copyright (C) 2022 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.

import math
from collections import defaultdict
from collections.abc import Mapping
from typing import List, Optional, Union

import torch
from mmcv import Config, ConfigDict
from mmdet.models.detectors import BaseDetector

from otx.algorithms.common.adapters.mmcv.utils import (
    get_meta_keys,
    is_epoch_based_runner,
    patch_color_conversion,
    prepare_work_dir,
    get_dataset_configs,
    get_configs_by_keys,
    update_config,
    remove_from_config,
)
from otx.algorithms.detection.configs.base import DetectionConfig
from otx.algorithms.detection.utils.data import (
    format_list_to_str,
    get_anchor_boxes,
    get_sizes_from_dataset_entity,
)
from otx.api.entities.datasets import DatasetEntity
from otx.api.entities.label import Domain, LabelEntity
from otx.api.usecases.reporting.time_monitor_callback import TimeMonitorCallback
from otx.api.utils.argument_checks import (
    DatasetParamTypeCheck,
    DirectoryPathCheck,
    check_input_parameters_type,
)
from otx.mpa.utils.logger import get_logger

try:
    from sklearn.cluster import KMeans

    __all__ = ["KMeans"]

    KMEANS_IMPORT = True
except ImportError:
    KMEANS_IMPORT = False


logger = get_logger()


@check_input_parameters_type({"work_dir": DirectoryPathCheck})
def patch_config(
    config: Config,
    work_dir: str,
    labels: List[LabelEntity],
    domain: Domain,
):  # pylint: disable=too-many-branches
    """Update config function."""
    # Set runner if not defined.
    if "runner" not in config:
        config.runner = ConfigDict({"type": "EpochBasedRunner"})

    # Check that there is no conflict in specification of number of training epochs.
    # Move global definition of epochs inside runner config.
    if "total_epochs" in config:
        if is_epoch_based_runner(config.runner):
            if config.runner.max_epochs != config.total_epochs:
                logger.warning("Conflicting declaration of training epochs number.")
            config.runner.max_epochs = config.total_epochs
        else:
            logger.warning(f"Total number of epochs set for an iteration based runner {config.runner.type}.")
        remove_from_config(config, "total_epochs")

    # Change runner's type.
    if is_epoch_based_runner(config.runner):
        logger.info(f"Replacing runner from {config.runner.type} to EpochRunnerWithCancel.")
        config.runner.type = "EpochRunnerWithCancel"
    else:
        logger.info(f"Replacing runner from {config.runner.type} to IterBasedRunnerWithCancel.")
        config.runner.type = "IterBasedRunnerWithCancel"

    # Add training cancelation hook.
    if "custom_hooks" not in config:
        config.custom_hooks = []
    if "CancelTrainingHook" not in {hook.type for hook in config.custom_hooks}:
        config.custom_hooks.append(ConfigDict({"type": "CancelTrainingHook"}))

    # Remove high level data pipelines definition leaving them only inside `data` section.
    remove_from_config(config, "train_pipeline")
    remove_from_config(config, "test_pipeline")
    # Patch data pipeline, making it OTX-compatible.
    patch_datasets(config, domain)

    # Remove FP16 config if running on CPU device and revert to FP32
    # https://github.com/pytorch/pytorch/issues/23377
    if not torch.cuda.is_available() and "fp16" in config:
        logger.info("Revert FP16 to FP32 on CPU device")
        remove_from_config(config, "fp16")

    if "log_config" not in config:
        config.log_config = ConfigDict()
    if "evaluation" not in config:
        config.evaluation = ConfigDict()
    evaluation_metric = config.evaluation.get("metric")
    if evaluation_metric is not None:
        config.evaluation.save_best = evaluation_metric
    if "checkpoint_config" not in config:
        config.checkpoint_config = ConfigDict()
    config.checkpoint_config.max_keep_ckpts = 5
    config.checkpoint_config.interval = config.evaluation.get("interval", 1)

    set_data_classes(config, labels)

    config.gpu_ids = range(1)
    config.work_dir = work_dir


@check_input_parameters_type()
def patch_model_config(
    config: Config,
    labels: List[LabelEntity],
):
    set_num_classes(config, len(labels))


@check_input_parameters_type()
def set_hyperparams(config: Config, hyperparams: DetectionConfig):
    """Set function for hyperparams (DetectionConfig)."""
    config.data.samples_per_gpu = int(hyperparams.learning_parameters.batch_size)
    config.data.workers_per_gpu = int(hyperparams.learning_parameters.num_workers)
    config.optimizer.lr = float(hyperparams.learning_parameters.learning_rate)

    total_iterations = int(hyperparams.learning_parameters.num_iters)

    config.lr_config.warmup_iters = int(hyperparams.learning_parameters.learning_rate_warmup_iters)
    if config.lr_config.warmup_iters == 0:
        config.lr_config.warmup = None
    if is_epoch_based_runner(config.runner):
        config.runner.max_epochs = total_iterations
    else:
        config.runner.max_iters = total_iterations


@check_input_parameters_type()
def patch_adaptive_repeat_dataset(
    config: Union[Config, ConfigDict],
    num_samples: int,
    decay: float = -0.002,
    factor: float = 30,
):
    """Patch the repeat times and training epochs adatively.

    Frequent dataloading inits and evaluation slow down training when the
    sample size is small. Adjusting epoch and dataset repetition based on
    empirical exponential decay improves the training time by applying high
    repeat value to small sample size dataset and low repeat value to large
    sample.

    :param config: mmcv config
    :param num_samples: number of training samples
    :param decay: decaying rate
    :param factor: base repeat factor
    """
    data_train = config.data.train
    if data_train.type == "MultiImageMixDataset":
        data_train = data_train.dataset
    if data_train.type == "RepeatDataset" and getattr(data_train, "adaptive_repeat_times", False):
        if is_epoch_based_runner(config.runner):
            cur_epoch = config.runner.max_epochs
            new_repeat = max(round(math.exp(decay * num_samples) * factor), 1)
            new_epoch = math.ceil(cur_epoch / new_repeat)
            if new_epoch == 1:
                return
            config.runner.max_epochs = new_epoch
            data_train.times = new_repeat


@check_input_parameters_type()
def align_data_config_with_recipe(
    data_config: ConfigDict,
    config: Union[Config, ConfigDict]
):
    data_config = data_config.data
    config = config.data
    for subset in data_config.keys():
        subset_config = data_config.get(subset, {})
        for key in list(subset_config.keys()):
            found_config = get_configs_by_keys(
                config.get(subset),
                key,
                return_path=True
            )
            assert len(found_config) == 1
            value = subset_config.pop(key)
            path = list(found_config.keys())[0]
            update_config(subset_config, {path: value})


@check_input_parameters_type()
def prepare_for_training(
    config: Union[Config, ConfigDict],
    data_config: ConfigDict,
) -> Union[Config, ConfigDict]:
    """Prepare configs for training phase."""
    prepare_work_dir(config)

    train_num_samples = 0
    for subset in ["train", "val", "test"]:
        data_config_ = data_config.data.get(subset)
        config_ = config.data.get(subset)
        if data_config_ is None:
            continue
        for key in ["otx_dataset"]:
            found = get_configs_by_keys(data_config_, key, return_path=True)
            if len(found) == 0:
                continue
            assert len(found) == 1
            if subset == "train" and key == "otx_dataset":
                found_value = list(found.values())[0]
                if found_value:
                    train_num_samples = len(found_value)
            update_config(config_, found)

    if train_num_samples > 0:
        patch_adaptive_repeat_dataset(config, train_num_samples)

    return config


@check_input_parameters_type()
def set_data_classes(config: Config, labels: List[LabelEntity]):
    """Setter data classes into config."""
    # Save labels in data configs.
    for subset in ("train", "val", "test"):
        for cfg in get_dataset_configs(config, subset):
            cfg.labels = labels
            #  config.data[subset].labels = labels


@check_input_parameters_type()
def set_num_classes(config: Config, num_classes: int):
    # Set proper number of classes in model's detection heads.
    head_names = ("mask_head", "bbox_head", "segm_head")
    if "roi_head" in config.model:
        for head_name in head_names:
            if head_name in config.model.roi_head:
                if isinstance(config.model.roi_head[head_name], List):
                    for head in config.model.roi_head[head_name]:
                        head.num_classes = num_classes
                else:
                    config.model.roi_head[head_name].num_classes = num_classes
    else:
        for head_name in head_names:
            if head_name in config.model:
                config.model[head_name].num_classes = num_classes
    # FIXME. ?
    # self.config.model.CLASSES = label_names


@check_input_parameters_type()
def patch_datasets(config: Config, domain: Domain):
    """Update dataset configs."""

    def update_pipeline(cfg):
        for pipeline_step in cfg.pipeline:
            if pipeline_step.type == "LoadImageFromFile":
                pipeline_step.type = "LoadImageFromOTXDataset"
            if pipeline_step.type == "LoadAnnotations":
                pipeline_step.type = "LoadAnnotationFromOTXDataset"
                pipeline_step.domain = domain
                pipeline_step.min_size = cfg.pop("min_size", -1)
            if subset == "train" and pipeline_step.type == "Collect":
                pipeline_step = get_meta_keys(pipeline_step)
        patch_color_conversion(cfg.pipeline)

    assert "data" in config
    for subset in ("train", "val", "test"):
        cfgs = get_dataset_configs(config, subset)

        for cfg in cfgs:
            cfg.type = "MPADetDataset"
            cfg.domain = domain
            cfg.otx_dataset = None
            cfg.labels = None
            remove_from_config(cfg, "ann_file")
            remove_from_config(cfg, "img_prefix")
            remove_from_config(cfg, "classes")  # Get from DatasetEntity
            update_pipeline(cfg)

        # 'MultiImageMixDataset' wrapper dataset has pipeline as well
        # which we should update
        if len(cfgs) and config.data[subset].type == "MultiImageMixDataset":
            update_pipeline(config.data[subset])


def patch_evaluation(config: Config):
    """Update evaluation configs."""
    cfg = config.evaluation
    # CocoDataset.evaluate -> CustomDataset.evaluate
    cfg.pop("classwise", None)
    cfg.metric = "mAP"
    cfg.save_best = "mAP"
    # EarlyStoppingHook
    config.early_stop_metric = "mAP"


def should_cluster_anchors(model_cfg: Config):
    if (
        hasattr(model_cfg.model, "bbox_head")
        and hasattr(model_cfg.model.bbox_head, "anchor_generator")
        and getattr(
            model_cfg.model.bbox_head.anchor_generator,
            "reclustering_anchors",
            False,
        )
    ):
        return True
    return False


@check_input_parameters_type({"dataset": DatasetParamTypeCheck})
def cluster_anchors(model_config: Config, data_config: Config, dataset: DatasetEntity):
    """Update configs for cluster_anchors."""
    if not KMEANS_IMPORT:
        raise ImportError(
            "Sklearn package is not installed. To enable anchor boxes clustering, please install "
            "packages from requirements/optional.txt or just scikit-learn package."
        )

    logger.info("Collecting statistics from training dataset to cluster anchor boxes...")
    [target_wh] = [
        transforms.img_scale for transforms in data_config.data.test.pipeline if transforms.type == "MultiScaleFlipAug"
    ]
    prev_generator = model_config.model.bbox_head.anchor_generator
    group_as = [len(width) for width in prev_generator.widths]
    wh_stats = get_sizes_from_dataset_entity(dataset, list(target_wh))

    if len(wh_stats) < sum(group_as):
        logger.warning(
            f"There are not enough objects to cluster: {len(wh_stats)} were detected, while it should be "
            f"at least {sum(group_as)}. Anchor box clustering was skipped."
        )
        return

    widths, heights = get_anchor_boxes(wh_stats, group_as)
    logger.info(
        f"Anchor boxes widths have been updated from {format_list_to_str(prev_generator.widths)} "
        f"to {format_list_to_str(widths)}"
    )
    logger.info(
        f"Anchor boxes heights have been updated from {format_list_to_str(prev_generator.heights)} "
        f"to {format_list_to_str(heights)}"
    )
    config_generator = model_config.model.bbox_head.anchor_generator
    config_generator.widths, config_generator.heights = widths, heights

    model_config.model.bbox_head.anchor_generator = config_generator
