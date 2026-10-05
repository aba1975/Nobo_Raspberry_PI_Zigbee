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
// when every door and window is closed, blinking red when any is open,
// blinking blue when a sensor cannot be heard from, or when this display can
// no longer reach the Pi. How bright is the Pi's too (X-Display-Light-Level,
// set in Settings). Never on battery: the board sleeps between asks, and the
// lights are put out before it does.
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
#include <esp32-hal-rmt.h>
#include <esp_sleep.h>
#include <sys/time.h>
#include <time.h>

#define FIRMWARE_VERSION "1.3.0"

namespace {

constexpr uint32_t STALE_AFTER_SECONDS = 30 * 60;
constexpr uint32_t WIFI_TIMEOUT_MS = 20000;
constexpr uint32_t HTTP_TIMEOUT_MS = 20000;
// On USB the Pi holds an ask open this long while nothing changes. Below the
// Pi's own cap of 30 s, and the HTTP timeout is raised by it.
constexpr uint32_t USB_WAIT_SECONDS = 25;
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
constexpr int LIGHT_COUNT = 2;
// Until the Pi says otherwise, and for a Pi too old to say.
constexpr uint8_t DEFAULT_LIGHT_LEVEL = 18;
constexpr uint32_t LIGHT_STEP_MS = 100;
constexpr uint32_t LIGHT_ON_MS = 700;
constexpr uint32_t LIGHT_PERIOD_MS = 3000;
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
RTC_DATA_ATTR uint8_t rtcBssid[6] = {0};
RTC_DATA_ATTR int32_t rtcChannel = 0;

Config config;
Preferences prefs;
String serialLine;
String lastProblem;
uint32_t nextFetchAt = 0;
uint32_t nextPowerCheckAt = 0;
bool usbPower = false;

enum class Outcome { Updated, Unchanged, Failed };

enum class Light : uint8_t { Off, Green, Red, Blue };
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

void showPanel() {
  M5.Display.display();
  M5.Display.waitDisplay();
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

void drawProblem() {
  String last = rtcEverSucceeded ? formatTime(rtcLastSuccess) : String("not since power on");
  if (!last.length()) last = "unknown";
  drawNotice(ink(191, 0, 0), "Not up to date",
             "This screen cannot reach Nobo,\n"
             "so doors and windows may have\n"
             "changed since it last could.\n\n"
             "Last updated: " + last + "\n" + lastProblem,
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
    case Light::Red: return "red";
    case Light::Blue: return "blue";
    default: return "off";
  }
}

// A Pi too old to send the header, or anything not understood, is dark: a
// light is only ever the Pi's word, never a guess here.
void takeLight(const String &value) {
  if (value == "green") light = Light::Green;
  else if (value == "red") light = Light::Red;
  else if (value == "blue") light = Light::Blue;
  else light = Light::Off;
}

// Anything that is not a level from 1 to 255 leaves the last one in place.
void takeLightLevel(const String &value) {
  long level = value.toInt();
  if (level >= 1 && level <= 255) lightLevel = (uint8_t)level;
}

// Every LED the same colour, WS2812 timing at 100 ns ticks, green first.
void showLight(uint8_t r, uint8_t g, uint8_t b) {
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

// Waits in short steps and gives up as soon as the colour or the level
// changes, so a door opened shows at once, not at the end of a blink.
bool holdLight(uint32_t ms, Light was, uint8_t level) {
  for (uint32_t waited = 0; waited < ms; waited += LIGHT_STEP_MS) {
    vTaskDelay(pdMS_TO_TICKS(LIGHT_STEP_MS));
    if (light != was || lightLevel != level) return false;
  }
  return true;
}

// Green is held: all is well, and a steady light is calmer to live with.
// Red and blue blink, because they ask for something to be done.
void lightLoop(void *) {
  for (;;) {
    Light now = light;
    uint8_t level = lightLevel;
    if (now == Light::Off) {
      showLight(0, 0, 0);
      holdLight(LIGHT_PERIOD_MS, now, level);
      continue;
    }
    showLight(now == Light::Red ? level : 0, now == Light::Green ? level : 0,
              now == Light::Blue ? level : 0);
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
  if (!lightBus) lightBus = rmtInit(LIGHT_PIN, RMT_TX_MODE, RMT_MEM_64);
  if (!lightBus) return;
  rmtSetTick(lightBus, 100);
  xTaskCreatePinnedToCore(lightLoop, "lights", 4096, nullptr, 1, &lightTask, 0);
}

// The LEDs keep their colour without the processor, so they are put out
// before it sleeps or they would glow until the battery is flat.
void lightsOut() {
  if (lightTask) vTaskSuspend(lightTask);
  showLight(0, 0, 0);
}

// waitSeconds > 0 asks the Pi to hold the answer until a sensor changes.
Outcome fetch(uint32_t waitSeconds) {
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
                               "X-Display-Light-Level"};
  http.collectHeaders(keep, 5);
  http.addHeader("Authorization", "Bearer " + config.token);
  http.addHeader("X-Display-Firmware", FIRMWARE_VERSION);
  http.addHeader("X-Display-Power", usbPower ? "usb" : "battery");
  int battery = batteryPercent();
  if (battery >= 0) http.addHeader("X-Display-Battery", String(battery));
  if (rtcEtag[0]) http.addHeader("If-None-Match", rtcEtag);

  int status = http.GET();
  if (http.hasHeader("Date")) takeTime(http.header("Date"));
  if (http.hasHeader("X-Display-Interval")) takeInterval(http.header("X-Display-Interval"));
  if (status == 200 || status == 304) {
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
      lastProblem = "No answer: " + HTTPClient::errorToString(status);
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
  http.end();
  if (got != (size_t)length) {
    free(picture);
    lastProblem = "The picture was cut short";
    return Outcome::Failed;
  }

  M5.Display.startWrite();
  bool drawn = M5.Display.drawPng(picture, length, 0, 0);
  M5.Display.endWrite();
  free(picture);
  if (!drawn) {
    lastProblem = "The picture could not be read";
    return Outcome::Failed;
  }
  showPanel();
  strlcpy(rtcEtag, etag.c_str(), sizeof(rtcEtag));
  return Outcome::Updated;
}

// One ask, and what the panel should show after it.
Outcome refresh(uint32_t waitSeconds = 0) {
  if (!config.complete()) return Outcome::Failed;
  Outcome outcome = fetch(waitSeconds);
  time_t now = time(nullptr);
  if (outcome != Outcome::Failed) {
    rtcLastSuccess = now;
    rtcEverSucceeded = true;
    rtcProblemShown = false;
    lastProblem = "";
    Serial.printf("{\"fetched\":\"%s\"}\n", outcome == Outcome::Updated ? "updated" : "unchanged");
    return outcome;
  }
  JsonDocument doc;
  doc["fetched"] = "failed";
  doc["problem"] = lastProblem;
  serializeJson(doc, Serial);
  Serial.println();
  // A first failure is not news: Wi-Fi blinks. Half an hour of them is, or
  // any failure when nothing has been shown since the power came on.
  bool longGone = !rtcEverSucceeded ||
                  (rtcLastSuccess >= CLOCK_SET && now - rtcLastSuccess >= (time_t)STALE_AFTER_SECONDS);
  if (longGone) light = usbPower ? Light::Blue : Light::Off;
  if (longGone && !rtcProblemShown) {
    drawProblem();
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
    Serial.println("{\"ok\":true}");
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

bool anyButton() {
  M5.update();
  return M5.BtnA.wasPressed() || M5.BtnB.wasPressed() || M5.BtnC.wasPressed();
}

void listen(uint32_t ms) {
  uint32_t started = millis();
  while (millis() - started < ms) {
    pollSerial();
    delay(20);
  }
}

}  // namespace

void setup() {
  Serial.setRxBufferSize(8192);
  auto cfg = M5.config();
  cfg.serial_baudrate = 115200;
  cfg.clear_display = false;
  M5.begin(cfg);
  // Exact colours only: the Pi already drew in the panel's six inks.
  M5.Display.setEpdMode(epd_mode_t::epd_fastest);
  loadConfig();
  orient();
  WiFi.mode(WIFI_STA);
  usbPower = onUsbPower();

  esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
  bool woken = cause != ESP_SLEEP_WAKEUP_UNDEFINED;
  // A button press always redraws, so it can be seen to have done something.
  if (cause == ESP_SLEEP_WAKEUP_EXT1) rtcEtag[0] = '\0';

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
      pollSerial();
      delay(20);
    }
    if (!config.complete()) sleepFor(0);
  }

  if (!usbPower) {
    // A moment to catch a provision.py that is waiting for the board, but
    // not on a timer wake: nothing is plugged in then, and every wake costs.
    if (cause != ESP_SLEEP_WAKEUP_TIMER) listen(1500);
    refresh();
    sleepFor(config.batteryMinutes * 60);
  }
  startLights();
  nextFetchAt = millis();
}

void loop() {
  pollSerial();
  if (anyButton()) {
    rtcEtag[0] = '\0';
    nextFetchAt = millis();
  }
  if ((int32_t)(millis() - nextFetchAt) >= 0) {
    uint32_t asked = millis();
    Outcome outcome = refresh(USB_WAIT_SECONDS);
    // Ask again at once, so the next change is caught as it happens. Not
    // after a failure, nor after a 304 that came back without waiting (a Pi
    // that does not hold answers open), or it would be asked in a tight loop.
    bool held = millis() - asked >= USB_WAIT_SECONDS * 1000 / 2;
    bool again = outcome == Outcome::Updated || (outcome == Outcome::Unchanged && held);
    nextFetchAt = millis() + (again ? 0 : config.usbSeconds * 1000);
  }
  if ((int32_t)(millis() - nextPowerCheckAt) >= 0) {
    nextPowerCheckAt = millis() + POWER_CHECK_MS;
    usbPower = onUsbPower();
    // Unplugged: carry on as a battery display.
    if (!usbPower && config.complete()) sleepFor(config.batteryMinutes * 60);
  }
  delay(20);
}
