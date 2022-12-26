"""BaseTask for NNCF."""

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


import io
import json
import os
import tempfile
from collections.abc import Mapping
from copy import deepcopy
from functools import partial
from typing import List, Optional

import torch
from mmcv.utils import Config, ConfigDict
from mpa.utils.logger import get_logger

from otx.algorithms.common.adapters.mmcv.utils import (
    get_configs_by_keys,
    remove_from_config,
)
from otx.algorithms.common.adapters.nncf import (
    check_nncf_is_enabled,
    is_accuracy_aware_training_set,
)
from otx.algorithms.common.adapters.nncf.compression import (
    COMPRESSION_STATE_NAME,
    DATA_TO_BUILD_NAME,
    NNCF_STATE_NAME,
    STATE_TO_BUILD_NAME,
)
from otx.algorithms.common.adapters.nncf.config import compose_nncf_config
from otx.algorithms.common.utils.callback import OptimizationProgressCallback
from otx.api.configuration import cfg_helper
from otx.api.configuration.helper.utils import ids_to_strings
from otx.api.entities.datasets import DatasetEntity
from otx.api.entities.model import (
    ModelEntity,
    ModelFormat,
    ModelOptimizationType,
    ModelPrecision,
    OptimizationMethod,
)
from otx.api.entities.optimization_parameters import (
    OptimizationParameters,
    default_progress_callback,
)
from otx.api.entities.subset import Subset
from otx.api.entities.task_environment import TaskEnvironment
from otx.api.serialization.label_mapper import label_schema_to_bytes
from otx.api.usecases.tasks.interfaces.optimization_interface import (
    IOptimizationTask,
    OptimizationType,
)
from otx.api.utils.argument_checks import (
    DatasetParamTypeCheck,
    check_input_parameters_type,
)

from .training_base import BaseTask


logger = get_logger()


class NNCFBaseTask(BaseTask, IOptimizationTask):
    @check_input_parameters_type()
    def __init__(self, task_environment: TaskEnvironment):
        super().__init__(task_environment)

        # Set default model attributes.
        check_nncf_is_enabled()
        self._nncf_data_to_build = None
        self._nncf_state_dict_to_build = dict()
        self._nncf_preset = None
        self._optimization_methods = []  # type: List
        self._precision = [ModelPrecision.FP32]

        self._scratch_space = tempfile.mkdtemp(prefix="otx-nncf-scratch-")
        logger.info(f"Scratch space created at {self._scratch_space}")

        # Extra control variables.
        self._training_work_dir = None
        self._is_training = False
        self._should_stop = False
        self._optimization_type = ModelOptimizationType.NNCF

        self._set_attributes_by_hyperparams()

        logger.info("Task initialization completed")

    def _set_attributes_by_hyperparams(self):
        quantization = self._hyperparams.nncf_optimization.enable_quantization
        pruning = self._hyperparams.nncf_optimization.enable_pruning
        if quantization and pruning:
            self._nncf_preset = "nncf_quantization_pruning"
            self._optimization_methods = [
                OptimizationMethod.QUANTIZATION,
                OptimizationMethod.FILTER_PRUNING,
            ]
            self._precision = [ModelPrecision.INT8]
            return
        if quantization and not pruning:
            self._nncf_preset = "nncf_quantization"
            self._optimization_methods = [OptimizationMethod.QUANTIZATION]
            self._precision = [ModelPrecision.INT8]
            return
        if not quantization and pruning:
            self._nncf_preset = "nncf_pruning"
            self._optimization_methods = [OptimizationMethod.FILTER_PRUNING]
            self._precision = [ModelPrecision.FP32]
            return
        raise RuntimeError("Not selected optimization algorithm")

    def _init_train_data_cfg(self, dataset: DatasetEntity):
        logger.info("init data cfg.")
        data_cfg = ConfigDict(
            data=ConfigDict(
                train=ConfigDict(
                    otx_dataset=dataset.get_subset(Subset.TRAINING),
                    labels=self._labels,
                ),
                val=ConfigDict(
                    otx_dataset=dataset.get_subset(Subset.VALIDATION),
                    labels=self._labels,
                ),
            )
        )

        # Temparory remedy for cfg.pretty_text error
        for label in self._labels:
            label.hotkey = "a"
        return data_cfg

    def _init_nncf_cfg(self):
        base_dir = os.path.abspath(os.path.dirname(self.template_file_path))
        nncf_config_path = os.path.join(base_dir, "compression_config.json")

        with open(nncf_config_path, encoding="UTF-8") as nncf_config_file:
            common_nncf_config = json.load(nncf_config_file)

        optimization_config = compose_nncf_config(
            common_nncf_config, [self._nncf_preset]
        )

        max_acc_drop = (
            self._hyperparams.nncf_optimization.maximal_accuracy_degradation / 100
        )
        if "accuracy_aware_training" in optimization_config["nncf_config"]:
            # Update maximal_absolute_accuracy_degradation
            (
                optimization_config["nncf_config"]["accuracy_aware_training"]["params"][
                    "maximal_absolute_accuracy_degradation"
                ]
            ) = max_acc_drop
            # Force evaluation interval
            self._recipe_cfg.evaluation.interval = 1
        else:
            logger.info("NNCF config has no accuracy_aware_training parameters")

        return ConfigDict(optimization_config)

    def _initialize_post_hook(self, options=dict()):
        super()._initialize_post_hook(options)
        assert self._recipe_cfg is not None and self._model_cfg is not None

        # TODO: more delicate configuration change control in MPA side

        # last batch size of 1 causes undefined behaviour for batch normalization
        # when initializing and training NNCF
        if self._data_cfg is not None:
            data_loader = self._recipe_cfg.data.get("train_dataloader", {})
            samples_per_gpu = data_loader.get(
                "samples_per_gpu", self._recipe_cfg.data.get("samples_per_gpu")
            )
            otx_dataset = get_configs_by_keys(self._data_cfg.data.train, "otx_dataset")
            assert len(otx_dataset) == 1
            otx_dataset = otx_dataset[0]
            if otx_dataset is not None and len(otx_dataset) % samples_per_gpu == 1:
                data_loader["drop_last"] = True
                self._recipe_cfg.data["train_dataloader"] = data_loader

        # nncf does not suppoer FP16
        if "fp16" in self._recipe_cfg or "fp16" in self._model_cfg:
            remove_from_config(self._recipe_cfg, "fp16")
            remove_from_config(self._model_cfg, "fp16")
            logger.warning("fp16 option is not supported in NNCF. Switch to fp32.")

        # merge nncf_cfg
        nncf_cfg = self._init_nncf_cfg()
        self._recipe_cfg.merge_from_dict(nncf_cfg)

        # configure nncf
        nncf_config = self._recipe_cfg.get("nncf_config", {})
        if nncf_config.get("target_metric_name", None) is None:
            metric_name = self._recipe_cfg.evaluation.metric
            if isinstance(metric_name, list):
                metric_name = metric_name[0]
            nncf_config.target_metric_name = metric_name
            logger.info(
                "'target_metric_name' not found in nncf config. "
                f"Using {metric_name} as target metric"
            )

        if is_accuracy_aware_training_set(nncf_config):
            # Prepare runner for Accuracy Aware
            self._recipe_cfg.runner = {
                "type": "AccuracyAwareRunner",
                "nncf_config": nncf_config,
            }

    @staticmethod
    def model_builder(
        config,
        *args,
        nncf_model_builder,
        model_config=None,
        data_config=None,
        is_export=False,
        return_compression_ctrl=False,
        **kwargs,
    ):
        if model_config is not None or data_config is not None:
            config = deepcopy(config)
            if model_config is not None:
                config.merge_from_dict(model_config)
            if data_config is not None:
                config.merge_from_dict(data_config)

        compression_ctrl, model, = nncf_model_builder(
            config,
            distributed=False,
            *args,
            **kwargs,
        )

        if is_export:
            compression_ctrl.prepare_for_export()
            model.disable_dynamic_graph_building()

        if return_compression_ctrl:
            return compression_ctrl, model
        return model

    def _optimize(
        self,
        dataset: DatasetEntity,
        optimization_parameters: Optional[OptimizationParameters] = None,
    ):
        raise NotImplementedError

    def _optimize_post_hook(
        self,
        dataset: DatasetEntity,
        output_model: ModelEntity,
    ):
        pass

    @check_input_parameters_type({"dataset": DatasetParamTypeCheck})
    def optimize(
        self,
        optimization_type: OptimizationType,
        dataset: DatasetEntity,
        output_model: ModelEntity,
        optimization_parameters: Optional[OptimizationParameters] = None,
    ):
        """NNCF Optimization."""
        if optimization_type is not OptimizationType.NNCF:
            raise RuntimeError("NNCF is the only supported optimization")

        if optimization_parameters is not None:
            update_progress_callback = optimization_parameters.update_progress
        else:
            update_progress_callback = default_progress_callback

        self._time_monitor = OptimizationProgressCallback(
            update_progress_callback,
            loading_stage_progress_percentage=5,
            initialization_stage_progress_percentage=5,
        )

        self._data_cfg = self._init_train_data_cfg(dataset)
        self._is_training = True

        results = self._optimize(dataset, optimization_parameters)

        # Check for stop signal when training has stopped.
        # If should_stop is true, training was cancelled
        if self._should_stop:
            logger.info("Training cancelled.")
            self._should_stop = False
            self._is_training = False
            return

        compression_state = torch.load(results.get("compression_state_path"))
        before_ckpt = torch.load(results.get("before_ckpt_path"))
        final_ckpt = torch.load(results.get("final_ckpt"))

        model_ckpt = {
            NNCF_STATE_NAME: final_ckpt["state_dict"],
            COMPRESSION_STATE_NAME: compression_state,
            DATA_TO_BUILD_NAME: before_ckpt["meta"][DATA_TO_BUILD_NAME],
            STATE_TO_BUILD_NAME: before_ckpt["meta"][STATE_TO_BUILD_NAME],
        }

        # update checkpoint to the newly trained model
        self._model_ckpt = os.path.join(
            os.path.dirname(results.get("final_ckpt")), "temporary.pth"
        )
        torch.save(model_ckpt, self._model_ckpt)

        self._optimize_post_hook(dataset, output_model)

        self.save_model(output_model)

        output_model.model_format = ModelFormat.BASE_FRAMEWORK
        output_model.optimization_type = self._optimization_type
        output_model.optimization_methods = self._optimization_methods
        output_model.precision = self._precision

        self._is_training = False

    def _save_model_post_hook(self, modelinfo):
        pass

    @check_input_parameters_type()
    def save_model(self, output_model: ModelEntity):
        """Saving model function for NNCF Task."""
        buffer = io.BytesIO()
        hyperparams_str = ids_to_strings(
            cfg_helper.convert(self._hyperparams, dict, enum_to_str=True)
        )
        labels = {label.name: label.color.rgb_tuple for label in self._labels}

        # some custom hooks are not pickable
        custom_hooks = self._recipe_cfg.pop("custom_hooks")
        config = deepcopy(self._recipe_cfg)
        config.merge_from_dict(self._model_cfg)
        self._recipe_cfg.custom_hooks = custom_hooks

        def update(d, u):
            for k, v in u.items():
                if isinstance(v, Mapping):
                    d[k] = update(d.get(k, {}), v)
                else:
                    d[k] = v
            return d

        modelinfo = torch.load(self._model_ckpt, map_location=torch.device("cpu"))
        modelinfo = update(
            dict(model=modelinfo),
            {
                "meta": {
                    "nncf_enable_compression": True,
                    "config": config,
                },
                "config": hyperparams_str,
                "labels": labels,
                "VERSION": 1,
            },
        )
        self._save_model_post_hook(modelinfo)

        torch.save(modelinfo, buffer)
        output_model.set_data("weights.pth", buffer.getvalue())
        output_model.set_data(
            "label_schema.json",
            label_schema_to_bytes(self._task_environment.label_schema),
        )
