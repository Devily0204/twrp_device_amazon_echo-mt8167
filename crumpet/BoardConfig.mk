#
# Copyright (C) 2026 The Team Win Recovery Project
#
# SPDX-License-Identifier: Apache-2.0
#

DEVICE_PATH := device/amazon/mt8167-echo/crumpet
DEVICE_ARCH := arm

include device/amazon/mt8167-echo/BoardConfigCommon.mk

# Crumpet is raw NAND. Do not package the common eMMC init, fstab or BCB scripts.
TARGET_RECOVERY_DEVICE_DIRS := $(COMMON_PATH)/leds $(DEVICE_PATH)
TARGET_RECOVERY_FSTAB := $(DEVICE_PATH)/recovery/root/etc/recovery.fstab

# These are unconditional imports from the stock TWRP init.rc. The comment-only
# configs preserve them for excluded logd, vold and nano; package them directly
# through AOSP product-copy rules rather than repacking recovery.img afterwards.
PRODUCT_COPY_FILES += \
    $(DEVICE_PATH)/recovery/root/init.recovery.logd.rc:recovery/root/init.recovery.logd.rc \
    $(DEVICE_PATH)/recovery/root/init.recovery.nano.rc:recovery/root/init.recovery.nano.rc \
    $(DEVICE_PATH)/recovery/root/init.recovery.vold_decrypt.rc:recovery/root/init.recovery.vold_decrypt.rc

# Stock Android boot headers (independently present in both Crumpet backups).
# The boot-image page size remains 2048; physical NAND pages are 4096 bytes.
BOARD_KERNEL_CMDLINE := bootopt=64S3,32N2,32N2 androidboot.selinux=permissive
BOARD_KERNEL_BASE := 0x40000000
BOARD_MKBOOTIMG_ARGS := --base $(BOARD_KERNEL_BASE) --kernel_offset 0x00008000 --ramdisk_offset 0x04000000 --second_offset 0x00f00000 --tags_offset 0x0e000000
BOARD_FLASH_BLOCK_SIZE := 262144
BOARD_BOOTIMAGE_PARTITION_SIZE := 15990784
# Artifact size ceiling only: Crumpet has no physical recovery/swdl partition.
BOARD_RECOVERYIMAGE_PARTITION_SIZE := 15990784

# Keep native A/B booting; Crumpet uses its own NAND OTA/BCB helpers.
AB_OTA_UPDATER := false
AB_OTA_PARTITIONS :=
PRODUCT_STATIC_BOOT_CONTROL_HAL :=
TARGET_RECOVERY_DEVICE_MODULES := crumpet-ubi crumpet-mtd crumpet-bootchain
TW_INCLUDE_BCBTOOL := false
TW_OTA_ASSERT_DEVICES := crumpet
# This old recovery tests whether the variable is nonempty, not whether it is true.
RECOVERY_SDCARD_ON_DATA :=

# Fit the NAND boot-image ceiling with the tools used by this headless profile.
# OTA extraction runs on the host; recovery scripts use /sbin/sh and ADB/USB-OTG.
TW_EXCLUDE_PYTHON := true
TW_EXCLUDE_BASH := true
TW_EXCLUDE_NANO := true
TW_EXCLUDE_MTP := true
TARGET_USERIMAGES_USE_F2FS := false

# Android 9 libbacktrace dependencies omitted by the legacy relink file list.
TARGET_RECOVERY_DEVICE_MODULES += libunwindstack libdexfile liblzma
TW_RECOVERY_ADDITIONAL_RELINK_FILES += \
    $(TARGET_OUT_SHARED_LIBRARIES)/libunwindstack.so \
    $(TARGET_OUT_SHARED_LIBRARIES)/libdexfile.so \
    $(TARGET_OUT_SHARED_LIBRARIES)/liblzma.so

TW_CRUMPET_NAND := true
ifeq ($(wildcard bootable/recovery/crumpet-nand-support.version),)
$(error Run python3 device/amazon/mt8167-echo/tools/prepare_recovery.py bootable/recovery before lunch omni_crumpet-eng)
endif
