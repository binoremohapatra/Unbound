// adc_front.h - 4-channel ADC front end: oversampled reads within tick budget.
#pragma once
#include <Arduino.h>
#include <stdint.h>

class AdcFront {
public:
    void begin();
    // Average `oversample` reads of channel ch (0..3). Returns counts 0..4095.
    int32_t read(int ch);
    // microseconds spent in the last read() call (for diag)
    uint32_t last_us() const { return last_us_; }
    int oversample() const { return ovs_; }

private:
    int ovs_ = 4;
    uint32_t last_us_ = 0;

    // ADC2 Wi-Fi Conflict Bypass for CH4 (GPIO 26)
    int32_t ch1_last_envelope_ = 0;
    int32_t ch4_spike_acc_ = 0;

    // Peak-to-peak 15ms window tracking for ADC1 channels (CH1, CH2, CH3)
    uint16_t ch_min_[3] = {4095, 4095, 4095};
    uint16_t ch_max_[3] = {0, 0, 0};
    uint32_t p2p_win_start_us_[3] = {0, 0, 0};
    int32_t p2p_val_[3] = {0, 0, 0};
};
