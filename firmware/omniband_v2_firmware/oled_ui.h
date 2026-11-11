// oled_ui.h - SSD1306 128x64 status display. Runs in loop() at ~30 Hz.
#pragma once
#include <Arduino.h>

void oled_begin();
void oled_show_msg(const char* text, int ms);
void oled_set_gesture(const char* name);
void oled_update();   // call every loop iteration; self-throttles to 30 Hz
