// mpu_front.h - Hardware MPU6050 Gyroscope front-end for OmniBand V2
#pragma once
#include <Arduino.h>
#include <Wire.h>
#include "hw_config.h"

class MpuFront {
public:
    void begin();
    void read(float &dx, float &dy);
    bool is_available() const { return available_; }

private:
    bool available_ = false;
    float gyro_bias_x_ = 0.0f;
    float gyro_bias_z_ = 0.0f;
};

extern MpuFront g_mpu;
