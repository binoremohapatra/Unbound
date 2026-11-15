#pragma once
#include "Arduino.h"
struct TwoWire { bool begin(int, int); void setClock(uint32_t); };
extern TwoWire Wire;
