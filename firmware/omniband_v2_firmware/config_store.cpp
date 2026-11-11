// config_store.cpp - Preferences (NVS) backend for PersistCfg.
#include "config_store.h"
#include <Preferences.h>
#include "constants_gen.h"

namespace k = omniband::constants;

namespace cfgstore {
namespace {
Preferences prefs;
constexpr const char* NS = "omni";
}

void begin() { prefs.begin(NS, false); }

void set_defaults(PersistCfg& c) {
    c.mode = 0;
    for (int i = 0; i < 4; ++i) c.thr_on[i] = (float)k::kThresholdDefaultOn[i];
    c.compat_v1 = false;
}

void load(PersistCfg& c) {
    set_defaults(c);
    c.mode = prefs.getInt("mode", 0);
    if (c.mode < 0 || c.mode >= k::kModeCount) c.mode = 0;
    for (int i = 0; i < 4; ++i) {
        char key[4]; snprintf(key, sizeof(key), "t%d", i);
        float v = prefs.getFloat(key, c.thr_on[i]);
        if (v >= (float)k::kThresholdMinOnCounts) c.thr_on[i] = v;
    }
    c.compat_v1 = prefs.getBool("c1", false);
}

void save(const PersistCfg& c) {
    prefs.putInt("mode", c.mode);
    for (int i = 0; i < 4; ++i) {
        char key[4]; snprintf(key, sizeof(key), "t%d", i);
        prefs.putFloat(key, c.thr_on[i]);
    }
    prefs.putBool("c1", c.compat_v1);
}
}  // namespace cfgstore
