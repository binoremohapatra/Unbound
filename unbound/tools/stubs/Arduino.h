// Minimal stub of the Arduino-ESP32 API: ONLY for g++ -fsyntax-only checks of the .ino on a PC.
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <math.h>
#define ESP_ARDUINO_VERSION_MAJOR 3
#define HIGH 1
#define LOW 0
#define OUTPUT 1
#define configTICK_RATE_HZ 1000
#define configMAX_PRIORITIES 25
typedef uint32_t TickType_t;
#define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
typedef struct { int dummy; } portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED {0}
#define portENTER_CRITICAL(m) ((void)(m))
#define portEXIT_CRITICAL(m) ((void)(m))
typedef void (*TaskFunction_t)(void*);
typedef void* TaskHandle_t;
typedef int BaseType_t;
typedef enum { ADC_0db, ADC_2_5db, ADC_6db, ADC_11db } adc_attenuation_t;
TickType_t xTaskGetTickCount();
void vTaskDelayUntil(TickType_t*, TickType_t);
void vTaskDelay(TickType_t);
void vTaskDelete(TaskHandle_t);
BaseType_t xTaskCreatePinnedToCore(TaskFunction_t, const char*, uint32_t, void*, int, TaskHandle_t*, int);
uint32_t micros(); uint32_t millis(); void delay(uint32_t);
void pinMode(uint8_t, uint8_t); void digitalWrite(uint8_t, uint8_t);
int analogRead(uint8_t); void analogReadResolution(uint8_t);
void analogSetPinAttenuation(uint8_t, adc_attenuation_t);
struct HardwareSerial {
  void setTxBufferSize(size_t); void begin(unsigned long);
  int availableForWrite(); size_t write(const uint8_t*, size_t);
  int available(); int read();
  size_t print(const char*); size_t print(unsigned long); size_t print(unsigned);
};
extern HardwareSerial Serial;
// --- queue API (V2 Bluetooth) ---
typedef void* QueueHandle_t;
#define pdTRUE 1
QueueHandle_t xQueueCreate(uint32_t, uint32_t);
BaseType_t xQueueSend(QueueHandle_t, const void*, TickType_t);
BaseType_t xQueueReceive(QueueHandle_t, void*, TickType_t);
uint32_t uxQueueMessagesWaiting(QueueHandle_t);
