#pragma once
// hw_config.h - OmniBand V2 hardware configuration & static assertions
#include <cstdint>
#include "constants_gen.h"

namespace omniband {
namespace hw {

// ─── CHANNEL PHYSICAL LAYOUT (anatomically correct) ────────────────────────
// CH1 (GPIO 32 / ADC1_CH4) → LEFT TEMPLE  (Temporalis muscle) → Jaw Clench
// CH2 (GPIO 34 / ADC1_CH6) → RIGHT TEMPLE (Temporalis muscle) → Secondary
// CH3 (GPIO 33 / ADC1_CH5) → FOREHEAD     (Frontalis muscle)  → Eyebrow Raise
// CH4 (GPIO 26 / ADC2_CH9) → DISABLED: ADC2 unusable while Wi-Fi is active
//
// Anatomy note:
//   Jaw clench fires the TEMPORALIS (temple) muscle — NOT the forehead.
//   Eyebrow raise fires the FRONTALIS (forehead) muscle.
// ─────────────────────────────────────────────────────────────────────────────
constexpr int PIN_CH1 = 32; // LEFT TEMPLE  (ADC1_CH4) — Temporalis → Jaw Clench
constexpr int PIN_CH2 = 34; // RIGHT TEMPLE (ADC1_CH6) — Temporalis → Secondary
constexpr int PIN_CH3 = 33; // FOREHEAD     (ADC1_CH5) — Frontalis  → Eyebrow Raise
constexpr int PIN_CH4 = 26; // DISABLED     (ADC2_CH9) — Wi-Fi conflict, digital-only fallback

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

