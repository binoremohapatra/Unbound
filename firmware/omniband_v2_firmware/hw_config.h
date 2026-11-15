#pragma once
// hw_config.h - OmniBand V2 hardware configuration & static assertions
#include <cstdint>
#include "constants_gen.h"

namespace omniband {
namespace hw {

// ─── CHANNEL PHYSICAL LAYOUT (updated) ─────────────────────────────────────
// CH1 (GPIO 32 / ADC1_CH4) → LEFT side muscle   → Scroll UP
// CH2 (GPIO 34 / ADC1_CH6) → RIGHT side muscle  → Scroll DOWN
// CH3 (GPIO 33 / ADC1_CH5) → FOREHEAD (primary) → Jaw Clench / Click / Hold
// CH4 (GPIO 26 / ADC2_CH9) → DISABLED: ADC2 unusable while Wi-Fi is active
// ─────────────────────────────────────────────────────────────────────────────
constexpr int PIN_CH1 = 32; // LEFT side muscle  (ADC1_CH4) — Scroll UP
constexpr int PIN_CH2 = 34; // RIGHT side muscle (ADC1_CH6) — Scroll DOWN
constexpr int PIN_CH3 = 33; // FOREHEAD primary  (ADC1_CH5) — Jaw Clench / Click
constexpr int PIN_CH4 = 26; // DISABLED          (ADC2_CH9) — Wi-Fi conflict, digital-only fallback

// I2C MPU6050 & OLED (SSD1306 128x64)
constexpr int PIN_I2C_SDA = 21;
constexpr int PIN_I2C_SCL = 22;
constexpr uint8_t MPU_I2C_ADDR = 0x68;
constexpr uint8_t OLED_I2C_ADDR = 0x3C;
constexpr uint32_t OLED_I2C_FREQ_HZ = 400000;

// Communication
constexpr uint32_t BAUD_BOOT = constants::kLinkBaudBoot;
constexpr uint32_t BAUD_FAST = constants::kLinkBaudFast;
constexpr const char* BT_DEVICE_NAME = "OmniBand";
constexpr const char* WIFI_AP_SSID = "OmniBand_WiFi";
constexpr const char* WIFI_AP_PASS = "12345678";
constexpr uint16_t WIFI_UDP_PORT = 4210;

// Timing & FreeRTOS validation
static_assert(constants::kTimingDspHz == 500, "DSP sampling rate must be 500 Hz");
static_assert(constants::kTimingDspTickUs == 2000, "DSP tick interval must be 2000 us");
static_assert(constants::kAdcChannels == 4, "System requires exactly 4 ADC channels");

} // namespace hw
} // namespace omniband

