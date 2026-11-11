#pragma once
// hw_config.h - OmniBand V2 hardware configuration & static assertions
#include <cstdint>
#include "constants_gen.h"

namespace omniband {
namespace hw {

// Pin mapping (ESP32 DevKit 30-pin, ADC1 only)
constexpr int PIN_CH1 = 32; // Left temple (ADC1_CH4)
constexpr int PIN_CH2 = 33; // Centre-left forehead (ADC1_CH5)
constexpr int PIN_CH3 = 34; // Centre-right forehead (ADC1_CH6 - input only)
constexpr int PIN_CH4 = 35; // Right temple / molar clench (ADC1_CH7 - input only)

// I2C OLED (SSD1306 128x64)
constexpr int PIN_I2C_SDA = 21;
constexpr int PIN_I2C_SCL = 22;
constexpr uint8_t OLED_I2C_ADDR = 0x3C;
constexpr uint32_t OLED_I2C_FREQ_HZ = 400000;

// Communication
constexpr uint32_t BAUD_BOOT = constants::kLinkBaudBoot;
constexpr uint32_t BAUD_FAST = constants::kLinkBaudFast;
constexpr const char* BT_DEVICE_NAME = "OmniBand-V2";

// Timing & FreeRTOS validation
static_assert(constants::kTimingDspHz == 500, "DSP sampling rate must be 500 Hz");
static_assert(constants::kTimingDspTickUs == 2000, "DSP tick interval must be 2000 us");
static_assert(constants::kAdcChannels == 4, "System requires exactly 4 ADC channels");

} // namespace hw
} // namespace omniband

