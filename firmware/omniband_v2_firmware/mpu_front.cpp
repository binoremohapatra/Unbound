// mpu_front.cpp - Direct register access for MPU6050 (Zero external libraries)
#include "mpu_front.h"

MpuFront g_mpu;

void MpuFront::begin() {
    Serial.println("[MPU] Probing MPU6050 on I2C (SDA=21, SCL=22)...");
    Wire.begin(omniband::hw::PIN_I2C_SDA, omniband::hw::PIN_I2C_SCL);
    Wire.setTimeOut(50);

    Wire.beginTransmission(omniband::hw::MPU_I2C_ADDR);
    byte err = Wire.endTransmission();
    if (err == 0) {
        Serial.println("[MPU] MPU6050 detected at address 0x68!");

        // Wake up MPU6050 (PWR_MGMT_1 register 0x6B = 0x00)
        Wire.beginTransmission(omniband::hw::MPU_I2C_ADDR);
        Wire.write(0x6B);
        Wire.write(0x00);
        Wire.endTransmission();

        // Configure Gyro full scale +/- 500 deg/s (GYRO_CONFIG register 0x1B = 0x08)
        Wire.beginTransmission(omniband::hw::MPU_I2C_ADDR);
        Wire.write(0x1B);
        Wire.write(0x08);
        Wire.endTransmission();

        // Measure stationary bias
        Serial.println("[MPU] Calibrating gyro zero-bias...");
        float sumX = 0, sumZ = 0;
        for (int i = 0; i < 30; i++) {
            Wire.beginTransmission(omniband::hw::MPU_I2C_ADDR);
            Wire.write(0x43);
            Wire.endTransmission(false);
            Wire.requestFrom((uint8_t)omniband::hw::MPU_I2C_ADDR, (uint8_t)6);
            if (Wire.available() >= 6) {
                int16_t gx = (Wire.read() << 8) | Wire.read();
                int16_t gy = (Wire.read() << 8) | Wire.read();
                int16_t gz = (Wire.read() << 8) | Wire.read();
                sumX += (float)gx;
                sumZ += (float)gz;
            }
            delay(10);
        }
        gyro_bias_x_ = sumX / 30.0f;
        gyro_bias_z_ = sumZ / 30.0f;
        available_ = true;
        Serial.println("[MPU] MPU6050 Ready! Hardware cursor motion enabled.");
    } else {
        Serial.println("[MPU] MPU6050 NOT detected. Optical head-mouse fallback will be used.");
        available_ = false;
    }
}

void MpuFront::read(float &dx, float &dy) {
    float pitch = 0.0f, roll = 0.0f;
    read(dx, dy, pitch, roll);
}

void MpuFront::read(float &dx, float &dy, float &pitch, float &roll) {
    dx = 0.0f;
    dy = 0.0f;
    pitch = 0.0f;
    roll = 0.0f;
    if (!available_) return;

    // Read 14 bytes starting at ACCEL_XOUT_H (0x3B)
    Wire.beginTransmission(omniband::hw::MPU_I2C_ADDR);
    Wire.write(0x3B);
    if (Wire.endTransmission(false) != 0) {
        available_ = false;
        return;
    }
    Wire.requestFrom((uint8_t)omniband::hw::MPU_I2C_ADDR, (uint8_t)14);
    if (Wire.available() >= 14) {
        int16_t ax = (Wire.read() << 8) | Wire.read();
        int16_t ay = (Wire.read() << 8) | Wire.read();
        int16_t az = (Wire.read() << 8) | Wire.read();
        (void)Wire.read(); (void)Wire.read(); // skip temp
        int16_t rawX = (Wire.read() << 8) | Wire.read(); // Pitch gyro
        int16_t rawY = (Wire.read() << 8) | Wire.read(); // Roll gyro
        int16_t rawZ = (Wire.read() << 8) | Wire.read(); // Yaw gyro

        // Compute pitch & roll angles in degrees
        float f_ax = (float)ax;
        float f_ay = (float)ay;
        float f_az = (float)az;
        pitch = atan2(f_ay, sqrt(f_ax * f_ax + f_az * f_az)) * 57.29578f;
        roll  = atan2(-f_ax, f_az) * 57.29578f;

        float gz = (float)rawZ - gyro_bias_z_;
        float gx = (float)rawX - gyro_bias_x_;

        // DEADZONE: ignore micro-tremors. SENSITIVITY: higher = slower/smoother cursor.
        // Increased from 175 → 350 to prevent violent/jumpy movement on real hardware.
        const float DEADZONE = 280.0f;
        const float SENSITIVITY = 350.0f;

        if (abs(gz) > DEADZONE) {
            float excess = (gz > 0) ? (gz - DEADZONE) : (gz + DEADZONE);
            dx = -(excess / SENSITIVITY);
        }
        if (abs(gx) > DEADZONE) {
            float excess = (gx > 0) ? (gx - DEADZONE) : (gx + DEADZONE);
            dy = (excess / SENSITIVITY);
        }

        // Direct firmware axis inversion to correct upside-down/inverted physical mounting
        dx = -dx;
        dy = -dy;
    }
}
