# Copyright (C) 2022 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#


from copy import deepcopy

from otx.algorithms.common.adapters.nncf.utils import is_nncf_enabled
from otx.algorithms.common.adapters.nncf.patchers import (
    NNCF_PATCHER,
    no_nncf_trace_wrapper,
)


if is_nncf_enabled():
    from nncf.torch.nncf_network import NNCFNetwork
    from otx.algorithms.common.adapters.nncf.patches import nncf_train_step

    # add wrapper train_step method
    NNCFNetwork.train_step = nncf_train_step


def evaluation_wrapper(self, fn, runner, *args, **kwargs):
    out = fn(runner, *args, **kwargs)
    setattr(runner, "all_metrics", deepcopy(runner.log_buffer.output))
    return out


NNCF_PATCHER.patch("mmcv.runner.EvalHook.evaluate", evaluation_wrapper)
NNCF_PATCHER.patch(
    "mpa.modules.hooks.eval_hook.CustomEvalHook.evaluate", evaluation_wrapper
)

NNCF_PATCHER.patch(
    "mpa.modules.hooks.recording_forward_hooks.FeatureVectorHook.func",
    no_nncf_trace_wrapper,
)
NNCF_PATCHER.patch(
    "mpa.modules.hooks.recording_forward_hooks.ActivationMapHook.func",
    no_nncf_trace_wrapper,
)
