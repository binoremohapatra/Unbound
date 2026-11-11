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
};
