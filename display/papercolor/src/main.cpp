// Nobø wall display for the M5Stack PaperColor.
//
// The Pi draws the picture (app/display_render.py); this only fetches it and
// puts it on the panel. Everything the display says about doors and windows
// is decided, worded and tested on the Pi, so the firmware rarely needs to
// change. What is decided here is the one thing the Pi cannot say: that the
// display cannot reach the Pi, and so is no longer showing the truth.
//
// The panel is redrawn only when a door, a window or one of their warnings has
// changed: the Pi's ETag is a fingerprint of the sensors, not of the picture,
// so every other ask is answered 304 and the panel is left alone.
//
// On USB power it stays awake and asks with ?wait=, which the Pi holds open
// until a sensor changes, so the panel follows a door within seconds. After a
// failure it tries again in `usb_seconds`. On battery it sleeps between asks
// for `battery_minutes`, which the Pi sends with every answer
// (X-Display-Interval, set in Settings), and any of the three buttons wakes
// it to ask at once.
//
// On USB its two lights show what the Pi says in X-Display-Light: steady green
// when every door and window is closed, blinking yellow when one is open but
// not yet left open, blinking red once it has been, blinking blue when a sensor cannot be heard from, or when this display can
// no longer reach the Pi. How bright is the Pi's too (X-Display-Light-Level,
// set in Settings). Never on battery: the board sleeps between asks, and the
// lights are put out before it does.
//
// On USB it never sleeps, so nothing would otherwise ever start it afresh:
// firmware 1.3.0 ran five days on the demo Pi and then failed every ask
// without sending a byte of TLS, which is what an exhausted or fragmented heap
// looks like. So it takes a one-second deep sleep - a clean heap and Wi-Fi,
// while the panel, the clock and what it remembers in RTC memory survive -
// after three failures in a row (at most once an hour), and once a day after
// an answer.
//
// Nothing secret is compiled in. Wi-Fi, address, key and the certificate the
// Pi's HTTPS is checked against arrive over USB from provision.py and are
// kept in NVS.

#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <M5Unified.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <driver/rtc_io.h>
#include <esp_heap_caps.h>
#include <esp32-hal-rmt.h>
#include <esp_sleep.h>
#include <esp_system.h>
#include <esp_task_wdt.h>
#include <sys/time.h>
#include <time.h>

#define FIRMWARE_VERSION "1.4.0"

namespace {

constexpr uint32_t STALE_AFTER_SECONDS = 30 * 60;
constexpr uint32_t WIFI_TIMEOUT_MS = 20000;
constexpr uint32_t HTTP_TIMEOUT_MS = 20000;
// On USB the Pi holds an ask open this long while nothing changes. Below the
// Pi's own cap of 30 s, and the HTTP timeout is raised by it.
constexpr uint32_t USB_WAIT_SECONDS = 25;
// E Ink asks for at least three minutes between refreshes of a Spectra 6
// panel. A newer picture than that waits; the lights do not.
constexpr uint32_t MIN_REFRESH_GAP_MS = 180000;
constexpr uint32_t MIN_BATTERY_MINUTES = 2;
constexpr uint32_t MAX_BATTERY_MINUTES = 240;
constexpr size_t MAX_PICTURE_BYTES = 256 * 1024;
constexpr int EMPTY_BATTERY_PERCENT = 5;
constexpr uint32_t EMPTY_RECHECK_SECONDS = 60 * 60;
// Above this the board is on USB power. The PMIC reports VBUS in millivolts.
constexpr int USB_VBUS_MV = 4000;
constexpr uint32_t POWER_CHECK_MS = 2000;
// How long a board that has not been set up listens on USB before sleeping.
constexpr uint32_t SETUP_LISTEN_MS = 10 * 60 * 1000;
// The lights: green held, red and blue a short glow every few seconds, not a
// beacon. Two WS2812s on
// GPIO 21, powered by the PMIC's LDO, which M5.begin() turns on. Driven here
// rather than through M5.Led: on this Arduino core (2.x, ESP-IDF 4.4)
// M5Unified's LED bus is an empty stub that accepts colours and sends none.
constexpr int LIGHT_PIN = 21;
// The panel's BUSY line, low while a refresh is still running (M5GFX's own
// pin for the PaperColor's ED2208).
constexpr int PANEL_BUSY_PIN = 11;
constexpr int LIGHT_COUNT = 2;
// The loop must come round within this, or the board restarts. Longer than
// any one turn can take: an ask held 30 s, TLS, and two refreshes that M5GFX
// gives up on after 40 s each. On 18 October 2026 a display stopped mid-redraw
// and stayed stopped for seven hours, its button dead, until power-cycled.
constexpr uint32_t WATCHDOG_SECONDS = 300;
// Until the Pi says otherwise, and for a Pi too old to say.
constexpr uint8_t DEFAULT_LIGHT_LEVEL = 18;
constexpr uint32_t LIGHT_STEP_MS = 100;
constexpr uint32_t LIGHT_ON_MS = 700;
constexpr uint32_t LIGHT_ACK_MS = 250;
constexpr uint32_t LIGHT_PERIOD_MS = 3000;
// A fresh start on USB: after this many failed asks in a row, but not more
// often than the gap, so a Pi that is simply down is not answered with a
// restart every minute and a half; and once a day regardless.
constexpr uint32_t FRESH_START_AFTER_FAILURES = 3;
constexpr time_t FRESH_START_GAP_SECONDS = 60 * 60;
constexpr uint32_t USB_FRESH_START_MS = 24UL * 60 * 60 * 1000;
// Wide enough for the notice screen's line.
constexpr size_t PROBLEM_CHARS = 40;
// Anything ever set by the Pi's Date header is after this.
constexpr time_t CLOCK_SET = 1700000000;

const gpio_num_t BUTTONS[] = {GPIO_NUM_10, GPIO_NUM_9, GPIO_NUM_1};

const char *DEFAULT_TZ = "CET-1CEST,M3.5.0,M10.5.0/3";

struct Config {
  String ssid;
  String password;
  String url;
  String token;
  String ca;
  String tz;
  uint32_t batteryMinutes = 15;
  uint32_t usbSeconds = 30;
  int rotation = -1;

  bool complete() const {
    return ssid.length() && url.startsWith("https://") && token.length() && ca.length();
  }
};

// Kept across deep sleep, lost when the power goes.
RTC_DATA_ATTR char rtcEtag[72] = "";
RTC_DATA_ATTR time_t rtcLastSuccess = 0;
RTC_DATA_ATTR bool rtcEverSucceeded = false;
RTC_DATA_ATTR bool rtcProblemShown = false;
RTC_DATA_ATTR bool rtcEmptyShown = false;
// The last refresh, sent with the next ask in X-Display-Refresh: on battery
// the board sleeps straight after drawing, so it is told on the next wake.
// The count tells the Pi a new refresh from the same one sent again.
RTC_DATA_ATTR uint32_t rtcRefreshCount = 0;
RTC_DATA_ATTR uint32_t rtcRefreshMs = 0;
RTC_DATA_ATTR bool rtcRefreshBusy = false;
RTC_DATA_ATTR int8_t rtcRefreshChipC = 0;
RTC_DATA_ATTR uint8_t rtcBssid[6] = {0};
RTC_DATA_ATTR int32_t rtcChannel = 0;
RTC_DATA_ATTR bool rtcFreshStarted = false;
RTC_DATA_ATTR time_t rtcLastFreshStart = 0;

Config config;
Preferences prefs;
String serialLine;
String lastProblem;
uint32_t nextFetchAt = 0;
bool buttonAsked = false;
// Set from an interrupt: on USB the loop spends nearly all its time inside an
// ask the Pi holds open, and a press over before it returns would be missed.
volatile bool buttonInterrupt = false;
// Tells the light task to flash once, so a press is seen to have landed.
volatile bool lightAck = false;
uint32_t nextPowerCheckAt = 0;
uint32_t failuresInARow = 0;
bool usbPower = false;

enum class Outcome { Updated, Deferred, Unchanged, Failed };

enum class Light : uint8_t { Off, Green, Yellow, Red, Blue };
// Written by the loop, read by the light task; a byte is written whole.
volatile Light light = Light::Off;
volatile uint8_t lightLevel = DEFAULT_LIGHT_LEVEL;
TaskHandle_t lightTask = nullptr;
rmt_obj_t *lightBus = nullptr;

// -- configuration -------------------------------------------------------------

// Asking NVS for a key it does not have logs an error on every boot; ask first.
String stored(const char *key, const char *fallback) {
  return prefs.isKey(key) ? prefs.getString(key, fallback) : String(fallback);
}

void loadConfig() {
  // Read-write so a blank device's namespace exists rather than failing to open.
  prefs.begin("nobo", false);
  config.ssid = stored("ssid", "");
  config.password = stored("password", "");
  config.url = stored("url", "");
  config.token = stored("token", "");
  config.ca = stored("ca", "");
  config.tz = stored("tz", DEFAULT_TZ);
  config.batteryMinutes = prefs.isKey("battery_min") ? prefs.getUInt("battery_min", 15) : 15;
  config.usbSeconds = prefs.isKey("usb_sec") ? prefs.getUInt("usb_sec", 30) : 30;
  config.rotation = prefs.isKey("rotation") ? prefs.getInt("rotation", -1) : -1;
  prefs.end();
  setenv("TZ", config.tz.c_str(), 1);
  tzset();
}

bool onUsbPower() { return M5.Power.getVBUSVoltage() > USB_VBUS_MV; }

int batteryPercent() {
  int level = M5.Power.getBatteryLevel();
  return level < 0 || level > 100 ? -1 : level;
}

// -- the panel -------------------------------------------------------------------

uint32_t ink(uint8_t r, uint8_t g, uint8_t b) { return M5.Display.color888(r, g, b); }

void orient() {
  if (config.rotation >= 0) {
    M5.Display.setRotation(config.rotation & 3);
  } else if (M5.Display.width() > M5.Display.height()) {
    M5.Display.setRotation(1);
  }
}

// When the panel last refreshed, since this boot. On battery every wake is a
// boot, and wakes are at least two minutes apart anyway.
bool refreshedThisBoot = false;
uint32_t lastRefreshMs = 0;

const char *resetReasonName();
bool resetReported = false;
bool restartedUnexpectedly();

uint32_t lastRefreshTookMs = 0;
bool lastRefreshCutShort = false;

void showPanel() {
  int8_t chipC = (int8_t)roundf(temperatureRead());
  uint32_t started = millis();
  M5.Display.display();
  M5.Display.waitDisplay();
  lastRefreshTookMs = millis() - started;
  lastRefreshCutShort = digitalRead(PANEL_BUSY_PIN) == LOW;
  Serial.printf("{\"refresh_ms\":%lu,\"busy_after\":%s}\n", (unsigned long)lastRefreshTookMs,
                lastRefreshCutShort ? "true" : "false");
  rtcRefreshCount++;
  rtcRefreshMs = lastRefreshTookMs;
  rtcRefreshBusy = lastRefreshCutShort;
  rtcRefreshChipC = chipC;
  refreshedThisBoot = true;
  lastRefreshMs = millis();
}

bool panelRested() {
  return !refreshedThisBoot || millis() - lastRefreshMs >= MIN_REFRESH_GAP_MS;
}

// A picture that came too soon after the last refresh, kept until the panel
// has rested. Its ETag is already in rtcEtag, so the Pi holds asks against it.
uint8_t *pendingPicture = nullptr;
size_t pendingLength = 0;

bool decodePicture(uint8_t *picture, size_t length) {
  M5.Display.startWrite();
  bool drawn = M5.Display.drawPng(picture, length, 0, 0);
  M5.Display.endWrite();
  return drawn;
}

// The panel is driven white before every picture. Drawn straight over a red
// band, the green that replaced it came out moss (16 October 2026): some red
// stays behind. Decoded once first, so a picture that cannot be read leaves
// the old one in place rather than a white panel.
bool drawPicture(uint8_t *picture, size_t length) {
  if (!decodePicture(picture, length)) return false;
  M5.Display.fillScreen(TFT_WHITE);
  showPanel();
  bool drawn = decodePicture(picture, length);
  showPanel();
  return drawn;
}

// The serial `cycle`: the whole panel black, white, red, yellow, blue and
// green in turn, a number of times, to work ink particles loose. One colour
// per three-minute rest, as E Ink asks, so it runs alongside everything else
// rather than blocking: five loops take an hour and a half.
constexpr uint8_t MAX_CYCLE_LOOPS = 10;
uint16_t cycleStep = 0;
uint16_t cycleSteps = 0;

bool cycling() { return cycleStep < cycleSteps; }

void endCycle(const char *why) {
  Serial.printf("{\"cycle\":\"%s\",\"done\":%u,\"of\":%u}\n", why, cycleStep, cycleSteps);
  cycleStep = cycleSteps = 0;
  // The picture comes back, drawn once the panel has rested.
  rtcEtag[0] = '\0';
  nextFetchAt = millis();
}

void cycleIfRested() {
  static const struct { uint8_t r, g, b; const char *name; } inks[] = {
      {0, 0, 0, "black"},  {255, 255, 255, "white"}, {191, 0, 0, "red"},
      {255, 243, 56, "yellow"}, {100, 64, 255, "blue"}, {67, 138, 28, "green"},
  };
  if (!cycling() || !panelRested()) return;
  const auto &next = inks[cycleStep % 6];
  M5.Display.fillScreen(ink(next.r, next.g, next.b));
  showPanel();
  cycleStep++;
  Serial.printf("{\"cycle\":\"%s\",\"step\":%u,\"of\":%u}\n", next.name, cycleStep, cycleSteps);
  if (!cycling()) endCycle("finished");
}

void stopCycle(const char *why) {
  if (cycling()) endCycle(why);
}

void dropPending() {
  free(pendingPicture);
  pendingPicture = nullptr;
  pendingLength = 0;
}

String formatTime(time_t when) {
  if (when < CLOCK_SET) return "";
  struct tm local;
  localtime_r(&when, &local);
  char text[32];
  strftime(text, sizeof(text), "%a %d %b, %H:%M", &local);
  return String(text);
}

// A screen this firmware draws itself, for the times the Pi cannot be asked.
// The fonts are ASCII, hence "Nobo".
void drawNotice(uint32_t band, const char *title, const String &lines, const String &foot) {
  const int w = M5.Display.width();
  const int h = M5.Display.height();
  const uint32_t white = ink(255, 255, 255);
  const uint32_t black = ink(0, 0, 0);
  M5.Display.startWrite();
  M5.Display.fillScreen(white);
  M5.Display.fillRect(0, 0, w, 150, band);
  M5.Display.setTextDatum(top_left);
  M5.Display.setTextColor(white, band);
  M5.Display.setFont(&fonts::FreeSansBold24pt7b);
  M5.Display.drawString(title, 18, 50);
  M5.Display.setTextColor(black, white);
  M5.Display.setFont(&fonts::FreeSans12pt7b);
  int y = 180;
  int start = 0;
  while (start <= (int)lines.length()) {
    int end = lines.indexOf('\n', start);
    if (end < 0) end = lines.length();
    M5.Display.drawString(lines.substring(start, end), 18, y);
    y += 34;
    start = end + 1;
  }
  M5.Display.drawFastHLine(18, h - 72, w - 36, black);
  M5.Display.drawString(foot, 18, h - 52);
  M5.Display.endWrite();
  showPanel();
}

// The six inks in bands, each named, to judge the panel's colours by eye.
void drawInks() {
  static const struct { uint8_t r, g, b; const char *name; } inks[] = {
      {0, 0, 0, "Black"},      {255, 255, 255, "White"}, {255, 243, 56, "Yellow"},
      {191, 0, 0, "Red"},      {100, 64, 255, "Blue"},   {67, 138, 28, "Green"},
  };
  const int w = M5.Display.width();
  const int band = M5.Display.height() / 6;
  M5.Display.startWrite();
  M5.Display.setFont(&fonts::FreeSansBold18pt7b);
  M5.Display.setTextDatum(middle_left);
  for (int i = 0; i < 6; i++) {
    uint32_t fill = ink(inks[i].r, inks[i].g, inks[i].b);
    bool light = i == 1 || i == 2;
    M5.Display.fillRect(0, i * band, w, band, fill);
    M5.Display.setTextColor(light ? ink(0, 0, 0) : ink(255, 255, 255), fill);
    M5.Display.drawString(inks[i].name, 18, i * band + band / 2);
  }
  M5.Display.drawRect(0, band, w, band, ink(0, 0, 0));
  M5.Display.endWrite();
  showPanel();
}

// asked: somebody pressed a button, so say that it was tried, or the press
// looks as if it did nothing.
void drawProblem(bool asked) {
  String last = rtcEverSucceeded ? formatTime(rtcLastSuccess) : String("not since power on");
  if (!last.length()) last = "unknown";
  String tried = "";
  if (asked) {
    tried = formatTime(time(nullptr));
    tried = "Tried again: " + (tried.length() ? tried : String("just now")) + "\n";
  }
  drawNotice(ink(191, 0, 0), "Not up to date",
             "This screen cannot reach Nobo,\n"
             "so doors and windows may have\n"
             "changed since it last could.\n\n"
             "Last updated: " + last + "\n" + tried + lastProblem,
             "Press a button to try again");
}

void drawSetup() {
  drawNotice(ink(100, 64, 255), "Set me up",
             "Connect this display to a computer\n"
             "with USB and run provision.py from\n"
             "display/papercolor in the Nobo\n"
             "repository.\n\n"
             "Device " + WiFi.macAddress(),
             "Firmware " FIRMWARE_VERSION);
}

void drawEmpty() {
  drawNotice(ink(191, 0, 0), "Battery empty",
             "This screen has stopped updating\n"
             "and is not showing the truth.\n\n"
             "Charge it with USB-C.",
             "It starts again once charged");
}

// -- the network -------------------------------------------------------------------

bool joinWifi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  bool remembered = rtcChannel > 0;
  if (remembered) {
    WiFi.begin(config.ssid.c_str(), config.password.c_str(), rtcChannel, rtcBssid);
  } else {
    WiFi.begin(config.ssid.c_str(), config.password.c_str());
  }
  uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < WIFI_TIMEOUT_MS) {
    // The access point may have moved channel: forget it and scan properly.
    if (remembered && millis() - started > 5000) {
      remembered = false;
      rtcChannel = 0;
      WiFi.disconnect();
      WiFi.begin(config.ssid.c_str(), config.password.c_str());
    }
    delay(100);
  }
  if (WiFi.status() != WL_CONNECTED) {
    rtcChannel = 0;
    lastProblem = "Wi-Fi " + config.ssid + " not joined";
    return false;
  }
  rtcChannel = WiFi.channel();
  memcpy(rtcBssid, WiFi.BSSID(), 6);
  return true;
}

// The Pi's Date header sets the clock, so "last updated" can be told.
void takeTime(const String &date) {
  struct tm parsed = {};
  if (!strptime(date.c_str(), "%a, %d %b %Y %H:%M:%S", &parsed)) return;
  setenv("TZ", "UTC0", 1);
  tzset();
  time_t utc = mktime(&parsed);
  setenv("TZ", config.tz.c_str(), 1);
  tzset();
  if (utc < CLOCK_SET) return;
  struct timeval now = {utc, 0};
  settimeofday(&now, nullptr);
}

// How often to wake on battery is the Pi's to say. Kept in NVS only when it
// changes, so a wake that hears the same number writes nothing to flash.
void takeInterval(const String &value) {
  long minutes = value.toInt();
  if (minutes < (long)MIN_BATTERY_MINUTES || minutes > (long)MAX_BATTERY_MINUTES) return;
  if ((uint32_t)minutes == config.batteryMinutes) return;
  config.batteryMinutes = (uint32_t)minutes;
  prefs.begin("nobo", false);
  prefs.putUInt("battery_min", config.batteryMinutes);
  prefs.end();
}

const char *lightName(Light value) {
  switch (value) {
    case Light::Green: return "green";
    case Light::Yellow: return "yellow";
    case Light::Red: return "red";
    case Light::Blue: return "blue";
    default: return "off";
  }
}

// A Pi too old to send the header, or anything not understood, is dark: a
// light is only ever the Pi's word, never a guess here.
void takeLight(const String &value) {
  if (value == "green") light = Light::Green;
  else if (value == "yellow") light = Light::Yellow;
  else if (value == "red") light = Light::Red;
  else if (value == "blue") light = Light::Blue;
  else light = Light::Off;
}

// Anything that is not a level from 1 to 255 leaves the last one in place.
void takeLightLevel(const String &value) {
  long level = value.toInt();
  if (level >= 1 && level <= 255) lightLevel = (uint8_t)level;
}

// Guards the LED bus, so lightsOut() cannot cut into a colour being sent:
// a WS2812 that gets half a frame takes it, and the next one keeps its own.
SemaphoreHandle_t lightBusLock = nullptr;

bool openLightBus() {
  if (lightBus) return true;
  lightBus = rmtInit(LIGHT_PIN, RMT_TX_MODE, RMT_MEM_64);
  if (!lightBus) return false;
  rmtSetTick(lightBus, 100);
  return true;
}

// Every LED the same colour, WS2812 timing at 100 ns ticks, green first.
void sendLight(uint8_t r, uint8_t g, uint8_t b) {
  if (!lightBus) return;
  rmt_data_t bits[24 * LIGHT_COUNT];
  const uint8_t bytes[3] = {g, r, b};
  int i = 0;
  for (int led = 0; led < LIGHT_COUNT; led++) {
    for (uint8_t value : bytes) {
      for (int bit = 7; bit >= 0; bit--) {
        bool one = value & (1 << bit);
        bits[i].level0 = 1;
        bits[i].duration0 = one ? 8 : 4;
        bits[i].level1 = 0;
        bits[i].duration1 = one ? 4 : 8;
        i++;
      }
    }
  }
  rmtWriteBlocking(lightBus, bits, i);
}

void showLight(uint8_t r, uint8_t g, uint8_t b) {
  if (lightBusLock) xSemaphoreTake(lightBusLock, portMAX_DELAY);
  sendLight(r, g, b);
  if (lightBusLock) xSemaphoreGive(lightBusLock);
}

// Waits in short steps and gives up as soon as the colour or the level
// changes, so a door opened shows at once, not at the end of a blink.
bool holdLight(uint32_t ms, Light was, uint8_t level) {
  for (uint32_t waited = 0; waited < ms; waited += LIGHT_STEP_MS) {
    vTaskDelay(pdMS_TO_TICKS(LIGHT_STEP_MS));
    if (light != was || lightLevel != level || lightAck) return false;
  }
  return true;
}

// One light's colour at a drive level. Yellow is red with some green: equal
// parts look lime on a WS2812, whose green die is the brightest of the three.
void showColour(Light which, uint8_t level) {
  switch (which) {
    case Light::Green: showLight(0, level, 0); break;
    case Light::Yellow: {
      uint8_t green = (uint8_t)((level * 3 + 4) / 5);
      showLight(level, green ? green : 1, 0);
      break;
    }
    case Light::Red: showLight(level, 0, 0); break;
    case Light::Blue: showLight(0, 0, level); break;
    default: showLight(0, 0, 0); break;
  }
}

// Green is held: all is well, and a steady light is calmer to live with.
// Yellow, red and blue blink, because they ask for something to be done.
void lightLoop(void *) {
  for (;;) {
    Light now = light;
    uint8_t level = lightLevel;
    if (lightAck) {
      lightAck = false;
      // Not when the light is off: off may be the owner's choice.
      if (now != Light::Off) {
        showLight(level, level, level);
        vTaskDelay(pdMS_TO_TICKS(LIGHT_ACK_MS));
        showLight(0, 0, 0);
        vTaskDelay(pdMS_TO_TICKS(LIGHT_ACK_MS));
      }
      continue;
    }
    if (now == Light::Off) {
      showLight(0, 0, 0);
      holdLight(LIGHT_PERIOD_MS, now, level);
      continue;
    }
    showColour(now, level);
    if (now == Light::Green) {
      holdLight(LIGHT_PERIOD_MS, now, level);
      continue;
    }
    if (!holdLight(LIGHT_ON_MS, now, level)) continue;
    showLight(0, 0, 0);
    holdLight(LIGHT_PERIOD_MS - LIGHT_ON_MS, now, level);
  }
}

// Its own task, so the lights keep blinking through an ask the Pi holds open.
void startLights() {
  if (lightTask) return;
  if (!openLightBus()) return;
  if (!lightBusLock) lightBusLock = xSemaphoreCreateMutex();
  xTaskCreatePinnedToCore(lightLoop, "lights", 4096, nullptr, 1, &lightTask, 0);
}

// The LEDs keep their colour without the processor, so they are put out
// before it sleeps or they would glow until the battery is flat. Always, not
// only when the lights were running: on battery they never are, and a WS2812
// can latch a stray colour as power comes on. On 18 October 2026 the first of
// the two did, and stayed lit through every sleep. The line is then held low,
// so nothing floating on it can light one while the board sleeps.
void lightsOut() {
  if (lightTask) {
    if (lightBusLock) xSemaphoreTake(lightBusLock, pdMS_TO_TICKS(500));
    vTaskSuspend(lightTask);
  }
  if (openLightBus()) sendLight(0, 0, 0);
  rtc_gpio_init((gpio_num_t)LIGHT_PIN);
  rtc_gpio_set_direction((gpio_num_t)LIGHT_PIN, RTC_GPIO_MODE_OUTPUT_ONLY);
  rtc_gpio_set_level((gpio_num_t)LIGHT_PIN, 0);
  rtc_gpio_hold_en((gpio_num_t)LIGHT_PIN);
}

// waitSeconds > 0 asks the Pi to hold the answer until a sensor changes.
// asked: a button (or the serial refresh) prompted it. Somebody is standing
// at the panel, so the picture is drawn at once, rest or no rest; so is one
// the Pi sends with X-Display-Redraw: now, for "Redraw now" in Settings.
// Before 1.3.9 both were kept for the rest of the three minutes, and a press
// right after a door closed seemed to do nothing (20 October 2026).
Outcome fetch(uint32_t waitSeconds, bool asked) {
  if (!joinWifi()) return Outcome::Failed;
  WiFiClientSecure tls;
  tls.setCACert(config.ca.c_str());
  HTTPClient http;
  bool waiting = waitSeconds > 0 && rtcEtag[0];
  uint32_t timeout = HTTP_TIMEOUT_MS + (waiting ? waitSeconds * 1000 : 0);
  http.setTimeout(timeout);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  String url = config.url;
  if (waiting) url += String(url.indexOf('?') < 0 ? "?" : "&") + "wait=" + String(waitSeconds);
  if (!http.begin(tls, url)) {
    lastProblem = "Bad address";
    return Outcome::Failed;
  }
  static const char *keep[] = {"ETag", "Date", "X-Display-Interval", "X-Display-Light",
                               "X-Display-Light-Level", "X-Display-Redraw"};
  http.collectHeaders(keep, 6);
  http.addHeader("Authorization", "Bearer " + config.token);
  http.addHeader("X-Display-Firmware", FIRMWARE_VERSION);
  http.addHeader("X-Display-Power", usbPower ? "usb" : "battery");
  int battery = batteryPercent();
  if (battery >= 0) http.addHeader("X-Display-Battery", String(battery));
  if (rtcRefreshCount) {
    char refresh[64];
    snprintf(refresh, sizeof refresh, "n=%lu;ms=%lu;busy=%d;chip=%d",
             (unsigned long)rtcRefreshCount, (unsigned long)rtcRefreshMs, rtcRefreshBusy ? 1 : 0,
             (int)rtcRefreshChipC);
    http.addHeader("X-Display-Refresh", refresh);
  }
  // Why this boot began, until the Pi has heard it once.
  if (!resetReported) http.addHeader("X-Display-Reset", resetReasonName());
  if (rtcEtag[0]) http.addHeader("If-None-Match", rtcEtag);

  int status = http.GET();
  if (http.hasHeader("Date")) takeTime(http.header("Date"));
  if (http.hasHeader("X-Display-Interval")) takeInterval(http.header("X-Display-Interval"));
  if (status == 200 || status == 304) {
    resetReported = true;
    takeLightLevel(http.header("X-Display-Light-Level"));
    takeLight(http.header("X-Display-Light"));
  }
  if (status == 304) {
    http.end();
    return Outcome::Unchanged;
  }
  if (status != 200) {
    if (status == 401) {
      lastProblem = "The display key was refused";
    } else if (status < 0) {
      // "Connection refused" is all HTTPClient knows; the TLS layer knows why.
      char reason[96] = "";
      if (tls.lastError(reason, sizeof(reason)) != 0 && reason[0]) {
        lastProblem = "TLS: " + String(reason);
      } else {
        lastProblem = "No answer: " + HTTPClient::errorToString(status);
      }
      if (lastProblem.length() > PROBLEM_CHARS) lastProblem = lastProblem.substring(0, PROBLEM_CHARS);
    } else {
      lastProblem = "Nobo answered " + String(status);
    }
    http.end();
    return Outcome::Failed;
  }

  int length = http.getSize();
  if (length <= 0 || (size_t)length > MAX_PICTURE_BYTES) {
    lastProblem = "Unexpected picture size";
    http.end();
    return Outcome::Failed;
  }
  uint8_t *picture = (uint8_t *)ps_malloc(length);
  if (!picture) {
    lastProblem = "Out of memory";
    http.end();
    return Outcome::Failed;
  }
  WiFiClient *stream = http.getStreamPtr();
  size_t got = 0;
  uint32_t started = millis();
  while (got < (size_t)length && millis() - started < timeout) {
    size_t ready = stream->available();
    if (ready) {
      size_t want = (size_t)length - got;
      got += stream->readBytes(picture + got, ready < want ? ready : want);
    } else if (!http.connected()) {
      break;
    } else {
      delay(5);
    }
  }
  String etag = http.header("ETag");
  bool now = asked || http.header("X-Display-Redraw") == "now";
  http.end();
  if (got != (size_t)length) {
    free(picture);
    lastProblem = "The picture was cut short";
    return Outcome::Failed;
  }

  dropPending();
  if (now) stopCycle("asked");
  // Only on USB: a battery wake goes back to sleep, and would never draw it.
  if (usbPower && !now && (!panelRested() || cycling())) {
    pendingPicture = picture;
    pendingLength = length;
    strlcpy(rtcEtag, etag.c_str(), sizeof(rtcEtag));
    return Outcome::Deferred;
  }
  bool drawn = drawPicture(picture, length);
  free(picture);
  if (!drawn) {
    lastProblem = "The picture could not be read";
    return Outcome::Failed;
  }
  strlcpy(rtcEtag, etag.c_str(), sizeof(rtcEtag));
  return Outcome::Updated;
}

// Draws a picture that was kept back, once the panel has rested.
void drawPendingIfRested() {
  if (!pendingPicture || !panelRested() || cycling()) return;
  bool drawn = drawPicture(pendingPicture, pendingLength);
  dropPending();
  // Unreadable: forget its ETag, so the Pi sends it again.
  if (!drawn) rtcEtag[0] = '\0';
  Serial.printf("{\"drew\":\"%s\"}\n", drawn ? "deferred" : "failed");
}

// One ask, and what the panel should show after it. asked: a button (or the
// serial refresh) prompted it, so a failure is drawn even if already shown.
Outcome refresh(uint32_t waitSeconds = 0, bool asked = false) {
  if (!config.complete()) return Outcome::Failed;
  Outcome outcome = fetch(waitSeconds, asked);
  time_t now = time(nullptr);
  if (outcome != Outcome::Failed) {
    rtcLastSuccess = now;
    rtcEverSucceeded = true;
    rtcProblemShown = false;
    lastProblem = "";
    Serial.printf("{\"fetched\":\"%s\"}\n", outcome == Outcome::Updated    ? "updated"
                                           : outcome == Outcome::Deferred ? "deferred"
                                                                          : "unchanged");
    return outcome;
  }
  JsonDocument doc;
  doc["fetched"] = "failed";
  doc["problem"] = lastProblem;
  doc["largest_block"] = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  serializeJson(doc, Serial);
  Serial.println();
  // A first failure is not news: Wi-Fi blinks. Half an hour of them is, or
  // any failure when nothing has been shown since the power came on.
  bool longGone = !rtcEverSucceeded ||
                  (rtcLastSuccess >= CLOCK_SET && now - rtcLastSuccess >= (time_t)STALE_AFTER_SECONDS);
  if (longGone) light = usbPower ? Light::Blue : Light::Off;
  if ((longGone && !rtcProblemShown) || asked) {
    drawProblem(asked);
    rtcProblemShown = true;
    // Whatever the Pi says next must be drawn, even if it has not changed.
    rtcEtag[0] = '\0';
  }
  return outcome;
}

// -- provisioning over USB -----------------------------------------------------------

void status() {
  JsonDocument doc;
  doc["ok"] = true;
  doc["firmware"] = FIRMWARE_VERSION;
  doc["mac"] = WiFi.macAddress();
  doc["configured"] = config.complete();
  // Never the password or the key: only whether there is one.
  doc["ssid"] = config.ssid;
  doc["url"] = config.url;
  doc["has_password"] = config.password.length() > 0;
  doc["has_key"] = config.token.length() > 0;
  doc["has_ca"] = config.ca.length() > 0;
  doc["tz"] = config.tz;
  doc["battery_minutes"] = config.batteryMinutes;
  doc["usb_seconds"] = config.usbSeconds;
  doc["battery"] = batteryPercent();
  doc["vbus_mv"] = M5.Power.getVBUSVoltage();
  doc["light"] = lightName(light);
  doc["light_level"] = lightLevel;
  doc["lights_running"] = lightTask != nullptr;
  doc["wifi"] = WiFi.status() == WL_CONNECTED;
  doc["width"] = M5.Display.width();
  doc["height"] = M5.Display.height();
  doc["last_success"] = rtcEverSucceeded ? formatTime(rtcLastSuccess) : "";
  doc["problem"] = lastProblem;
  doc["uptime_s"] = millis() / 1000;
  doc["free_heap"] = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  // The chip's own, not the panel's, but it sits by the panel's connector:
  // a warm panel draws green towards yellow.
  doc["chip_c"] = (int)roundf(temperatureRead());
  doc["refresh_ms"] = lastRefreshTookMs;
  doc["reset_reason"] = resetReasonName();
  doc["busy_after"] = lastRefreshCutShort;
  if (cycling()) {
    doc["cycle_step"] = cycleStep;
    doc["cycle_of"] = cycleSteps;
  }
  doc["largest_block"] = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  doc["last_fresh_start"] = rtcFreshStarted ? formatTime(rtcLastFreshStart) : "";
  serializeJson(doc, Serial);
  Serial.println();
}

void configure(JsonDocument &in) {
  prefs.begin("nobo", false);
  // Only what is sent changes, so the interval can be adjusted alone.
  for (const char *key : {"ssid", "password", "url", "token", "ca", "tz"}) {
    if (in[key].is<const char *>()) prefs.putString(key, in[key].as<const char *>());
  }
  if (in["battery_minutes"].is<uint32_t>()) {
    prefs.putUInt("battery_min", constrain(in["battery_minutes"].as<uint32_t>(), 2u, 240u));
  }
  if (in["usb_seconds"].is<uint32_t>()) {
    prefs.putUInt("usb_sec", constrain(in["usb_seconds"].as<uint32_t>(), 15u, 3600u));
  }
  if (in["rotation"].is<int>()) prefs.putInt("rotation", constrain(in["rotation"].as<int>(), -1, 3));
  prefs.end();
  loadConfig();
  orient();
  WiFi.disconnect();
  rtcChannel = 0;
  rtcEtag[0] = '\0';
  rtcProblemShown = false;
  status();
  nextFetchAt = millis();
}

void forget() {
  prefs.begin("nobo", false);
  prefs.clear();
  prefs.end();
  loadConfig();
  rtcEtag[0] = '\0';
  rtcEverSucceeded = false;
  rtcChannel = 0;
  WiFi.disconnect();
  status();
  drawSetup();
}

void handleLine(const String &line) {
  JsonDocument in;
  if (deserializeJson(in, line)) {
    Serial.println("{\"ok\":false,\"error\":\"not JSON\"}");
    return;
  }
  const char *cmd = in["cmd"] | "";
  if (!strcmp(cmd, "status")) {
    status();
  } else if (!strcmp(cmd, "config")) {
    configure(in);
  } else if (!strcmp(cmd, "refresh")) {
    rtcEtag[0] = '\0';
    nextFetchAt = millis();
    buttonAsked = true;
    Serial.println("{\"ok\":true}");
  } else if (!strcmp(cmd, "clean")) {
    // For a panel whose colours have gone muddy, as after a refresh cut short
    // by a reset: drive every pixel fully black, then fully white, then show
    // the six inks side by side, and the picture once the panel has rested.
    dropPending();
    M5.Display.fillScreen(TFT_BLACK);
    showPanel();
    M5.Display.fillScreen(TFT_WHITE);
    showPanel();
    drawInks();
    rtcEtag[0] = '\0';
    nextFetchAt = millis();
    Serial.println("{\"ok\":true}");
  } else if (!strcmp(cmd, "cycle")) {
    // loops 0 stops a cycle under way.
    int loops = in["loops"] | 5;
    if (loops < 0 || loops > MAX_CYCLE_LOOPS) {
      Serial.println("{\"ok\":false,\"error\":\"loops must be 0 to 10\"}");
    } else if (loops == 0) {
      stopCycle("stopped");
      Serial.println("{\"ok\":true}");
    } else {
      dropPending();
      cycleStep = 0;
      cycleSteps = loops * 6;
      Serial.printf("{\"ok\":true,\"steps\":%u,\"minutes\":%lu}\n", cycleSteps,
                    (unsigned long)(cycleSteps * MIN_REFRESH_GAP_MS / 60000));
    }
  } else if (!strcmp(cmd, "light")) {
    // For checking the LEDs by eye; the Pi's next answer replaces it.
    if (in["level"].is<int>()) takeLightLevel(String(in["level"].as<int>()));
    takeLight(String(in["color"] | "off"));
    Serial.printf("{\"ok\":true,\"light\":\"%s\",\"level\":%u}\n", lightName(light),
                  (unsigned)lightLevel);
  } else if (!strcmp(cmd, "forget")) {
    forget();
  } else {
    Serial.println("{\"ok\":false,\"error\":\"unknown command\"}");
  }
}

void pollSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      serialLine.trim();
      if (serialLine.length()) handleLine(serialLine);
      serialLine = "";
    } else if (serialLine.length() < 8192) {
      serialLine += c;
    }
  }
}

// -- sleep ---------------------------------------------------------------------------

const char *resetReasonName() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power_on";
    case ESP_RST_DEEPSLEEP: return "wake";
    case ESP_RST_SW: return "software";
    case ESP_RST_TASK_WDT: return "watchdog";
    case ESP_RST_INT_WDT: return "interrupt_watchdog";
    case ESP_RST_WDT: return "other_watchdog";
    case ESP_RST_PANIC: return "crash";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_EXT: return "reset_pin";
    default: return "other";
  }
}

bool restartedUnexpectedly() {
  esp_reset_reason_t why = esp_reset_reason();
  return why == ESP_RST_TASK_WDT || why == ESP_RST_INT_WDT || why == ESP_RST_WDT ||
         why == ESP_RST_PANIC || why == ESP_RST_BROWNOUT;
}

[[noreturn]] void sleepFor(uint32_t seconds) {
  lightsOut();
  Serial.flush();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  M5.Display.sleep();
  uint64_t mask = 0;
  // The pull-ups must hold through sleep, or a floating pin wakes it.
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_ON);
  for (gpio_num_t pin : BUTTONS) {
    rtc_gpio_pullup_en(pin);
    rtc_gpio_pulldown_dis(pin);
    mask |= 1ULL << pin;
  }
  esp_sleep_enable_ext1_wakeup(mask, ESP_EXT1_WAKEUP_ANY_LOW);
  if (seconds) esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);
  esp_deep_sleep_start();
}

// A deep sleep of a second: everything in RAM starts clean, and the panel,
// the clock and RTC memory (the ETag, the last success) carry on as after any
// battery wake. Used only on USB, where the board would otherwise never sleep.
[[noreturn]] void freshStart(const char *why) {
  rtcFreshStarted = true;
  rtcLastFreshStart = time(nullptr);
  Serial.printf("{\"fresh_start\":\"%s\"}\n", why);
  sleepFor(1);
}

bool freshStartAllowed() {
  return !rtcFreshStarted || time(nullptr) - rtcLastFreshStart >= FRESH_START_GAP_SECONDS;
}

void IRAM_ATTR onButton() {
  buttonInterrupt = true;
  lightAck = true;
}

bool anyButton() {
  M5.update();
  bool pressed = M5.BtnA.wasPressed() || M5.BtnB.wasPressed() || M5.BtnC.wasPressed();
  if (buttonInterrupt) {
    buttonInterrupt = false;
    pressed = true;
  }
  return pressed;
}

void listen(uint32_t ms) {
  uint32_t started = millis();
  while (millis() - started < ms) {
    esp_task_wdt_reset();
    pollSerial();
    delay(20);
  }
}

}  // namespace

void setup() {
  // Hand the buttons back from the sleep's RTC pull-ups to ordinary GPIO, or
  // they cannot be read after a wake that stays awake (on USB).
  for (gpio_num_t pin : BUTTONS) rtc_gpio_deinit(pin);
  rtc_gpio_hold_dis((gpio_num_t)LIGHT_PIN);
  rtc_gpio_deinit((gpio_num_t)LIGHT_PIN);
  // Replaces the core's settings: a longer timeout, and a restart, not a log.
  esp_task_wdt_init(WATCHDOG_SECONDS, true);
  esp_task_wdt_add(nullptr);
  Serial.setRxBufferSize(8192);
  auto cfg = M5.config();
  cfg.serial_baudrate = 115200;
  cfg.clear_display = false;
  M5.begin(cfg);
  // Exact colours only: the Pi already drew in the panel's six inks.
  M5.Display.setEpdMode(epd_mode_t::epd_fastest);
  // Refresh only in showPanel(), which times it; M5GFX would otherwise
  // refresh at the end of every drawing transaction.
  M5.Display.setAutoDisplay(false);
  loadConfig();
  orient();
  WiFi.mode(WIFI_STA);
  usbPower = onUsbPower();

  esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
  bool woken = cause != ESP_SLEEP_WAKEUP_UNDEFINED;
  // A button press always redraws, so it can be seen to have done something.
  if (cause == ESP_SLEEP_WAKEUP_EXT1) rtcEtag[0] = '\0';
  // Restarted by the watchdog or a crash, perhaps mid-refresh: RTC memory
  // survives that, so without this the Pi would answer 304 and the panel
  // stay half-drawn.
  if (restartedUnexpectedly()) rtcEtag[0] = '\0';

  int battery = batteryPercent();
  if (!usbPower && battery >= 0 && battery <= EMPTY_BATTERY_PERCENT) {
    if (!rtcEmptyShown) {
      drawEmpty();
      rtcEmptyShown = true;
    }
    sleepFor(EMPTY_RECHECK_SECONDS);
  }
  if (rtcEmptyShown) {
    // Charged again: whatever the Pi says must replace the empty screen.
    rtcEmptyShown = false;
    rtcEtag[0] = '\0';
  }

  if (!config.complete()) {
    if (!woken) drawSetup();
    uint32_t started = millis();
    while (!config.complete() && (onUsbPower() || millis() - started < SETUP_LISTEN_MS)) {
      esp_task_wdt_reset();
      pollSerial();
      delay(20);
    }
    if (!config.complete()) sleepFor(0);
  }

  if (!usbPower) {
    // A moment to catch a provision.py that is waiting for the board, but
    // not on a timer wake: nothing is plugged in then, and every wake costs.
    if (cause != ESP_SLEEP_WAKEUP_TIMER) listen(1500);
    refresh(0, cause == ESP_SLEEP_WAKEUP_EXT1);
    sleepFor(config.batteryMinutes * 60);
  }
  startLights();
  for (gpio_num_t pin : BUTTONS) {
    pinMode(pin, INPUT_PULLUP);
    attachInterrupt(pin, onButton, FALLING);
  }
  nextFetchAt = millis();
}

void loop() {
  esp_task_wdt_reset();
  pollSerial();
  if (anyButton()) {
    stopCycle("button");
    rtcEtag[0] = '\0';
    nextFetchAt = millis();
    buttonAsked = true;
    lightAck = true;
  }
  cycleIfRested();
  drawPendingIfRested();
  if ((int32_t)(millis() - nextFetchAt) >= 0) {
    uint32_t asked = millis();
    bool pressed = buttonAsked;
    buttonAsked = false;
    // With a picture waiting, hold no longer than the panel has left to rest.
    uint32_t wait = USB_WAIT_SECONDS;
    if (pendingPicture) {
      uint32_t left = (MIN_REFRESH_GAP_MS - (millis() - lastRefreshMs)) / 1000 + 1;
      if (left < wait) wait = left;
    }
    Outcome outcome = refresh(wait, pressed);
    // Ask again at once, so the next change is caught as it happens. Not
    // after a failure, nor after a 304 that came back without waiting (a Pi
    // that does not hold answers open), or it would be asked in a tight loop.
    bool held = millis() - asked >= wait * 1000 / 2;
    bool again = outcome == Outcome::Updated || outcome == Outcome::Deferred ||
                 (outcome == Outcome::Unchanged && held);
    nextFetchAt = millis() + (again ? 0 : config.usbSeconds * 1000);
    failuresInARow = outcome == Outcome::Failed ? failuresInARow + 1 : 0;
    if (failuresInARow >= FRESH_START_AFTER_FAILURES && freshStartAllowed()) freshStart("failures");
    if (outcome != Outcome::Failed && millis() >= USB_FRESH_START_MS) freshStart("daily");
  }
  if ((int32_t)(millis() - nextPowerCheckAt) >= 0) {
    nextPowerCheckAt = millis() + POWER_CHECK_MS;
    usbPower = onUsbPower();
    // Unplugged: carry on as a battery display.
    if (!usbPower && config.complete()) sleepFor(config.batteryMinutes * 60);
  }
  delay(20);
}
