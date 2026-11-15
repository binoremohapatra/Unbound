// adc_front.cpp
// ─── CHANNEL PHYSICAL LAYOUT ───────────────────────────────────────────────
//  CH1 (index 0 / GPIO 32 / ADC1) → LEFT  side muscle → Scroll UP
//  CH2 (index 1 / GPIO 34 / ADC1) → RIGHT side muscle → Scroll DOWN
//  CH3 (index 2 / GPIO 33 / ADC1) → FOREHEAD (primary jaw/clench) → Click / Hold
//  CH4 (index 3 / GPIO 26 / ADC2) → DISABLED while Wi-Fi is active;
//                                    kept as digital-spike fallback only
// ───────────────────────────────────────────────────────────────────────────
#include "adc_front.h"
#include "constants_gen.h"
#include "hw_config.h"

namespace k = omniband::constants;

void AdcFront::begin() {
    analogReadResolution(k::kAdcResolutionBits);
    analogSetAttenuation(ADC_11db);
    ovs_ = k::kAdcOversample;
    if (ovs_ < k::kAdcOversampleMin) ovs_ = k::kAdcOversampleMin;

    // Settle ADC1 pins (GPIO 32, 34, 33)
    for (int i = 0; i < 3; ++i) {
        int pin = k::kAdcPins[i];
        for (int d = 0; d < k::kAdcDummyReads; ++d) analogRead(pin);
        delayMicroseconds(k::kAdcSettleUs);
        ch_min_[i] = 4095;
        ch_max_[i] = 0;
        p2p_win_start_us_[i] = micros();
        p2p_val_[i] = 0;
    }

    // Configure CH4 (GPIO 26) as digital input for high-speed spike detection
    // bypassing ESP32 ADC2 Wi-Fi hardware controller conflict
    pinMode(omniband::hw::PIN_CH4, INPUT_PULLDOWN);
    ch1_last_envelope_ = 0;
    ch4_spike_acc_ = 0;
}

int32_t AdcFront::read(int ch) {
    if (ch < 0 || ch >= k::kAdcChannels) return 0;
    uint32_t t0 = micros();

    // -------------------------------------------------------------------------
    // Channel 4 (ch == 3): Hybrid Digital Spike + CH1 Temporalis Coupling
    // -------------------------------------------------------------------------
    if (ch == 3) {
        // 1. High-speed digital spike pulse counting on GPIO 26
        int spikes = 0;
        for (int i = 0; i < 16; ++i) {
            if (digitalRead(omniband::hw::PIN_CH4) == HIGH) {
                spikes++;
            }
        }

        if (spikes > 0) {
            ch4_spike_acc_ += spikes * 240;
            if (ch4_spike_acc_ > 4095) ch4_spike_acc_ = 4095;
        }

        // 2. Cross-channel temporalis coupling from CH1 (GPIO 32)
        // Jaw clenching strongly co-activates both temporalis and masseter muscles
        int32_t coupled = 0;
        if (ch1_last_envelope_ > 180) {
            coupled = (int32_t)((ch1_last_envelope_ - 150) * 1.25f);
        }

        int32_t total = ch4_spike_acc_;
        if (coupled > total) total = coupled;
        if (total > 4095) total = 4095;
        if (total < 0) total = 0;

        // Exponential decay of accumulated spike energy (fast attack, smooth release)
        ch4_spike_acc_ = (int32_t)(ch4_spike_acc_ * 0.90f);

        last_us_ = micros() - t0;
        return total;
    }

    // -------------------------------------------------------------------------
    // Channels 1..3 (ch == 0, 1, 2): Robust 1 kHz Peak-to-Peak Window Envelope
    // -------------------------------------------------------------------------
    int pin = k::kAdcPins[ch];
    for (int d = 0; d < k::kAdcDummyReads; ++d) (void)analogRead(pin);

    uint32_t sum = 0;
    uint16_t sample_min = 4095;
    uint16_t sample_max = 0;

    for (int i = 0; i < ovs_; ++i) {
        uint16_t val = (uint16_t)analogRead(pin);
        sum += val;
        if (val < sample_min) sample_min = val;
        if (val > sample_max) sample_max = val;
    }

    uint16_t avg = (uint16_t)(sum / (uint32_t)ovs_);

    // Update rolling 15ms window min/max
    if (sample_min < ch_min_[ch]) ch_min_[ch] = sample_min;
    if (sample_max > ch_max_[ch]) ch_max_[ch] = sample_max;

    uint32_t now_us = micros();
    if (now_us - p2p_win_start_us_[ch] >= 15000) {
        p2p_val_[ch] = (int32_t)(ch_max_[ch] - ch_min_[ch]);
        ch_min_[ch] = 4095;
        ch_max_[ch] = 0;
        p2p_win_start_us_[ch] = now_us;
    }

    int32_t out_val = avg;
    // Catch rapid somatic burst spikes: blend instantaneous sample with peak-to-peak amplitude
    if (p2p_val_[ch] > 100) {
        int32_t dynamic_spike = avg + (int32_t)(p2p_val_[ch] * 0.75f);
        if (dynamic_spike > out_val) out_val = dynamic_spike;
    }
    if (out_val > 4095) out_val = 4095;

    // Cache CH3 (forehead/primary) envelope for CH4 digital-spike cross-coupling.
    // CH3 is the new primary jaw-clench channel; coupling to CH4 fallback still valid.
    if (ch == 2) {
        ch1_last_envelope_ = out_val;  // variable reused — represents "primary channel envelope"
    }

    last_us_ = micros() - t0;
    return out_val;
}
