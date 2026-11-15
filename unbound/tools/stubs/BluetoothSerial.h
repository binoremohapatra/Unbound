// Stub for syntax checks only.
#pragma once
#include "Arduino.h"
struct BluetoothSerial {
  bool begin(const char*);
  bool hasClient();
  int available(); int read();
  size_t write(const uint8_t*, size_t);
};
