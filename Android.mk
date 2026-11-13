#
# Copyright (C) 2026 The Team Win Recovery Project
#
# SPDX-License-Identifier: Apache-2.0
#

LOCAL_PATH := $(call my-dir)

# The shared boot-control HAL assumes eMMC misc I/O, not Crumpet raw NAND.
ifneq ($(filter cupcake donut,$(TARGET_DEVICE)),)
include $(call all-subdir-makefiles,$(LOCAL_PATH))
else ifeq ($(TARGET_DEVICE),crumpet)
include $(LOCAL_PATH)/crumpet/nand/Android.mk
endif
