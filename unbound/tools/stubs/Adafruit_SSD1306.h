#pragma once
#include "Wire.h"
#include "Adafruit_GFX.h"
#define SSD1306_SWITCHCAPVCC 2
#define SSD1306_WHITE 1
struct Adafruit_SSD1306 {
  Adafruit_SSD1306(uint8_t w, uint8_t h, TwoWire* twi, int8_t rst);
  bool begin(uint8_t vcs, uint8_t addr);
  void clearDisplay(); void display(); void setTextSize(uint8_t); void setTextColor(uint16_t);
  void setCursor(int16_t, int16_t);
  size_t print(const char*); size_t print(float, int); size_t print(unsigned long); size_t print(unsigned); size_t print(int);
  void drawRect(int16_t, int16_t, int16_t, int16_t, uint16_t);
  void fillRect(int16_t, int16_t, int16_t, int16_t, uint16_t);
};
