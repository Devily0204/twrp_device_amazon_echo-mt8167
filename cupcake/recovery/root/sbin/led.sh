#!/sbin/sh

LED_DEV=/sys/bus/i2c/devices/0-0030
LED_COUNT=1

led_show() {
    echo "$1" > /sys/class/leds/R/brightness
    echo "$2" > /sys/class/leds/G/brightness
    echo "$3" > /sys/class/leds/B/brightness
}
