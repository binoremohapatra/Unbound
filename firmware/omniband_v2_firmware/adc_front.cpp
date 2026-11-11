// adc_front.cpp
#include "adc_front.h"
#include "constants_gen.h"

namespace k = omniband::constants;

void AdcFront::begin() {
    analogReadResolution(k::kAdcResolutionBits);
    analogSetAttenuation(ADC_11db);
    ovs_ = k::kAdcOversample;
    if (ovs_ < k::kAdcOversampleMin) ovs_ = k::kAdcOversampleMin;
    // settle the SAR + routing once per channel
    for (int i = 0; i < k::kAdcChannels; ++i) {
        int pin = k::kAdcPins[i];
        for (int d = 0; d < k::kAdcDummyReads; ++d) analogRead(pin);
        delayMicroseconds(k::kAdcSettleUs);
    }
}

int32_t AdcFront::read(int ch) {
    if (ch < 0 || ch >= k::kAdcChannels) return 0;
    int pin = k::kAdcPins[ch];
    uint32_t t0 = micros();
    uint32_t sum = 0;
    for (int d = 0; d < k::kAdcDummyReads; ++d) (void)analogRead(pin);
    for (int i = 0; i < ovs_; ++i) sum += (uint32_t)analogRead(pin);
    last_us_ = micros() - t0;
    return (int32_t)(sum / (uint32_t)ovs_);
}
