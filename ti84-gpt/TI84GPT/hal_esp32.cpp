// hal.h for the ESP32 (the desktop tests have their own version in test/).
#ifdef ARDUINO

#include <Arduino.h>
#include <stdarg.h>

#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "hal.h"

static SemaphoreHandle_t gStateMutex = nullptr;

void halBegin() {
  if (!gStateMutex) gStateMutex = xSemaphoreCreateMutex();
}

namespace hal {

// The link lines are driven "open drain": writing 0 pulls the wire to ground,
// writing 1 lets go of it. The pin never drives the wire high, so it cannot
// fight the calculator, and it stays readable the whole time.
void lineInit(int pin) {
  const gpio_num_t gpio = (gpio_num_t)pin;
  gpio_set_level(gpio, 1);  // "released" before the output is switched on
  gpio_config_t config = {};
  config.pin_bit_mask = 1ULL << pin;
  config.mode = GPIO_MODE_INPUT_OUTPUT_OD;
  config.pull_up_en = GPIO_PULLUP_ENABLE;
  config.pull_down_en = GPIO_PULLDOWN_DISABLE;
  config.intr_type = GPIO_INTR_DISABLE;
  gpio_config(&config);
  gpio_set_level(gpio, 1);
}

void lineLow(int pin) { gpio_set_level((gpio_num_t)pin, 0); }
void lineRelease(int pin) { gpio_set_level((gpio_num_t)pin, 1); }
bool lineHigh(int pin) { return gpio_get_level((gpio_num_t)pin) != 0; }

uint32_t micros() { return (uint32_t)::micros(); }
uint32_t millis() { return (uint32_t)::millis(); }
void sleepMs(uint32_t ms) { ::delay(ms); }
void relax() { vTaskDelay(1); }

void lock() {
  if (gStateMutex) xSemaphoreTake(gStateMutex, portMAX_DELAY);
}

void unlock() {
  if (gStateMutex) xSemaphoreGive(gStateMutex);
}

void logf(const char *fmt, ...) {
  char buffer[200];
  va_list args;
  va_start(args, fmt);
  vsnprintf(buffer, sizeof(buffer), fmt, args);
  va_end(args);
  Serial.println(buffer);
}

}  // namespace hal

#endif  // ARDUINO
