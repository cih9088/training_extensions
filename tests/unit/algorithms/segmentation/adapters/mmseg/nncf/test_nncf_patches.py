# Copyright (C) 2021-2023 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#

from tests.test_suite.e2e_test_system import e2e_pytest_unit

from mmseg.models.segmentors.base import BaseSegmentor

import otx.algorithms.segmentation.adapters.mmseg.nncf.patches


@e2e_pytest_unit
def test_patches():
    assert getattr(BaseSegmentor, "nncf_trace_context", None) is not None
