// config_store.h - NVS persistence for OmniBand V2 host-visible settings.
#pragma once
#include <Arduino.h>

struct PersistCfg {
    int32_t mode;
    float thr_on[4];
    bool compat_v1;
};

namespace cfgstore {
    void begin();
    void load(PersistCfg& out);       // missing keys -> defaults
    void save(const PersistCfg& in);
    void set_defaults(PersistCfg& out);
}
