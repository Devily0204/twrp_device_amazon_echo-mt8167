#
# Copyright (C) 2026 The Team Win Recovery Project
#
# SPDX-License-Identifier: Apache-2.0
#

DEVICE_PATH := device/amazon/mt8167-echo/donut
DEVICE_ARCH := arm64

# OTA
TW_OTA_ASSERT_DEVICES := donut,donut_puffin

include device/amazon/mt8167-echo/BoardConfigCommon.mk
