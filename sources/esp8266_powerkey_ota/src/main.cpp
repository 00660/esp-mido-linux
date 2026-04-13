#include <ArduinoOTA.h>
#include <ESP8266WebServer.h>
#include <ESP8266WiFi.h>
#include <LittleFS.h>

extern "C" {
#include "user_interface.h"
}

constexpr uint8_t STATUS_LED_PIN = 4;
constexpr uint8_t FAN_PIN = 12;
constexpr uint8_t POWER_KEY_PIN = 5;
constexpr uint8_t PROBE_PIN_A = 14;
constexpr uint8_t PROBE_PIN_B = 3;
constexpr unsigned long BOOT_HOLD_MS = 5000;
constexpr unsigned long SERIAL_BAUD = 115200;
constexpr unsigned long SUPERVISOR_CONTROL_PROBE_MS = 3500;
constexpr uint16_t PWM_RANGE = 1000;
constexpr uint16_t PWM_FREQ = 500;
constexpr uint16_t STATUS_LED_ON_DUTY = 100;  // 10%
constexpr uint32_t CONTROL_SYNC_MIN_US = 42000;
constexpr uint32_t CONTROL_SYNC_MAX_US = 120000;
constexpr uint32_t CONTROL_SYMBOL_BASE_US = 6000;
constexpr uint32_t CONTROL_SYMBOL_STEP_US = 2000;
constexpr uint32_t CONTROL_SYMBOL_TOLERANCE_US = 6000;
constexpr uint32_t CONTROL_LINK_TIMEOUT_MS = 4000;
constexpr uint8_t CONTROL_SYMBOL_MAX = 15;
constexpr uint8_t CONTROL_PROTOCOL_MAGIC = 0x0A;
constexpr uint8_t CONTROL_PROTOCOL_VERSION = 1;
constexpr uint8_t CONTROL_FRAME_HELLO = 1;
constexpr uint8_t CONTROL_FRAME_DATA = 2;
constexpr uint8_t CONTROL_FRAME_LEGACY = 3;
constexpr uint8_t CONTROL_FRAME_SYMBOLS = 8;

const char* AP_SSID = "mido-ctrl-setup";
const char* AP_PASS = "12345678";
const char* OTA_HOSTNAME = "mido-ctrl";
const char* OTA_PASSWORD = "12345678";
const char* WIFI_CFG_PATH = "/wifi.txt";

ESP8266WebServer server(80);

bool powerKeyReleased = false;
bool otaReady = false;
unsigned long bootPressStartMs = 0;
unsigned long lastWifiLogMs = 0;
uint8_t bootResetReason = REASON_DEFAULT_RST;
bool bootSawSupervisorReply = false;
bool bootPowerKeyPulseRequested = false;
String savedSsid;
String savedPass;
unsigned long lastLedToggleMs = 0;
bool ledBlinkState = false;
int lastProbeA = -1;
int lastProbeB = -1;
unsigned long probeChangeCountA = 0;
unsigned long probeChangeCountB = 0;
unsigned long lastProbeChangeMsA = 0;
unsigned long lastProbeChangeMsB = 0;
volatile uint32_t controlPulseStartedUs = 0;
volatile uint8_t controlSymbols[CONTROL_FRAME_SYMBOLS] = {0, 0, 0, 0, 0, 0, 0, 0};
volatile uint8_t controlSymbolCount = 0;
volatile bool controlFrameActive = false;
volatile uint8_t controlFanDuty = 0;
volatile uint8_t controlLedDuty = 0;
volatile uint32_t controlLastFrameMs = 0;
volatile uint32_t controlLastDataMs = 0;
volatile uint32_t controlFrameCount = 0;
volatile uint32_t controlHelloFrameCount = 0;
volatile uint32_t controlDataFrameCount = 0;
volatile uint8_t controlLastSeq = 0;
volatile uint8_t controlLastType = 0;
volatile bool controlProtocolSynced = false;

void updateStatusLed();
void setPowerKeyIdle();

String htmlEscape(const String& input) {
  String out;
  out.reserve(input.length() + 16);
  for (size_t i = 0; i < input.length(); ++i) {
    const char c = input[i];
    if (c == '&') out += F("&amp;");
    else if (c == '<') out += F("&lt;");
    else if (c == '>') out += F("&gt;");
    else if (c == '"') out += F("&quot;");
    else out += c;
  }
  return out;
}

String jsEscape(const String& input) {
  String out;
  out.reserve(input.length() + 16);
  for (size_t i = 0; i < input.length(); ++i) {
    const char c = input[i];
    if (c == '\\' || c == '\'') {
      out += '\\';
      out += c;
    } else if (c == '\r' || c == '\n') {
      out += ' ';
    } else {
      out += c;
    }
  }
  return out;
}

uint16_t dutyToPwm(uint8_t duty) {
  const uint32_t clamped = duty > 100 ? 100 : duty;
  return static_cast<uint16_t>((clamped * PWM_RANGE + 50) / 100);
}

uint8_t dutyFromByte(uint8_t value) {
  return static_cast<uint8_t>((static_cast<uint32_t>(value) * 100U + 127U) / 255U);
}

uint8_t dutyFromNibble(uint8_t value) {
  const uint8_t clamped = value > 15 ? 15 : value;
  return static_cast<uint8_t>((static_cast<uint32_t>(clamped) * 100U + 7U) / 15U);
}

const __FlashStringHelper* frameTypeName(uint8_t frameType) {
  if (frameType == CONTROL_FRAME_HELLO) {
    return F("hello");
  }
  if (frameType == CONTROL_FRAME_DATA) {
    return F("data");
  }
  if (frameType == CONTROL_FRAME_LEGACY) {
    return F("legacy");
  }
  return F("unknown");
}

const __FlashStringHelper* resetReasonName(uint8_t reason) {
  switch (reason) {
    case REASON_DEFAULT_RST:
      return F("default");
    case REASON_WDT_RST:
      return F("wdt");
    case REASON_EXCEPTION_RST:
      return F("exception");
    case REASON_SOFT_WDT_RST:
      return F("soft_wdt");
    case REASON_SOFT_RESTART:
      return F("soft_restart");
    case REASON_DEEP_SLEEP_AWAKE:
      return F("deep_sleep");
    case REASON_EXT_SYS_RST:
      return F("ext_sys");
    default:
      return F("unknown");
  }
}

bool allowPowerKeyFallback(uint8_t reason) {
  return reason == REASON_DEFAULT_RST || reason == REASON_EXT_SYS_RST;
}

int IRAM_ATTR decodeControlSymbol(uint32_t widthUs) {
  if (widthUs < 2500) {
    return -1;
  }
  if (widthUs > (CONTROL_SYMBOL_BASE_US + static_cast<uint32_t>(CONTROL_SYMBOL_MAX) * CONTROL_SYMBOL_STEP_US + CONTROL_SYMBOL_TOLERANCE_US)) {
    return -1;
  }
  const int symbol = static_cast<int>((static_cast<int32_t>(widthUs) - static_cast<int32_t>(CONTROL_SYMBOL_BASE_US) + static_cast<int32_t>(CONTROL_SYMBOL_STEP_US / 2)) / static_cast<int32_t>(CONTROL_SYMBOL_STEP_US));
  if (symbol < 0) {
    return 0;
  }
  if (symbol > CONTROL_SYMBOL_MAX) {
    return CONTROL_SYMBOL_MAX;
  }
  return symbol;
}

void IRAM_ATTR finalizePendingControlFrame(uint8_t symbolCount) {
  if (symbolCount == 3) {
    const uint8_t checksum = static_cast<uint8_t>((controlSymbols[0] ^ controlSymbols[1]) & 0x0F);
    if (checksum != controlSymbols[2]) {
      return;
    }

    controlProtocolSynced = true;
    controlLastSeq = 0;
    controlLastType = CONTROL_FRAME_LEGACY;
    controlLastFrameMs = millis();
    ++controlFrameCount;
    ++controlDataFrameCount;
    controlFanDuty = dutyFromNibble(controlSymbols[0]);
    controlLedDuty = dutyFromNibble(controlSymbols[1]);
    controlLastDataMs = controlLastFrameMs;
    return;
  }

  if (symbolCount != CONTROL_FRAME_SYMBOLS) {
    return;
  }

  const uint8_t checksum = static_cast<uint8_t>(
      (controlSymbols[0] ^ controlSymbols[1] ^ controlSymbols[2] ^ controlSymbols[3] ^
       controlSymbols[4] ^ controlSymbols[5] ^ controlSymbols[6]) &
      0x0F);
  if (checksum != controlSymbols[7]) {
    return;
  }

  const uint8_t magic = controlSymbols[0] & 0x0F;
  const uint8_t versionAndType = controlSymbols[1] & 0x0F;
  const uint8_t version = static_cast<uint8_t>((versionAndType >> 2) & 0x03);
  const uint8_t frameType = static_cast<uint8_t>(versionAndType & 0x03);
  const uint8_t seq = controlSymbols[2] & 0x0F;
  const uint8_t fanByte = static_cast<uint8_t>((controlSymbols[3] << 4) | controlSymbols[4]);
  const uint8_t ledByte = static_cast<uint8_t>((controlSymbols[5] << 4) | controlSymbols[6]);

  if (magic != CONTROL_PROTOCOL_MAGIC || version != CONTROL_PROTOCOL_VERSION) {
    controlProtocolSynced = false;
    return;
  }

  controlProtocolSynced = true;
  controlLastSeq = seq;
  controlLastType = frameType;
  controlLastFrameMs = millis();
  ++controlFrameCount;

  if (frameType == CONTROL_FRAME_HELLO) {
    ++controlHelloFrameCount;
    return;
  }

  if (frameType != CONTROL_FRAME_DATA) {
    return;
  }

  ++controlDataFrameCount;
  controlFanDuty = dutyFromByte(fanByte);
  controlLedDuty = dutyFromByte(ledByte);
  controlLastDataMs = controlLastFrameMs;
}

void IRAM_ATTR handleControlWireEdge() {
  const uint32_t nowUs = micros();
  const bool levelHigh = digitalRead(PROBE_PIN_A) != 0;

  if (!levelHigh) {
    controlPulseStartedUs = nowUs;
    return;
  }

  if (controlPulseStartedUs == 0) {
    return;
  }

  const uint32_t pulseWidthUs = nowUs - controlPulseStartedUs;
  controlPulseStartedUs = 0;

  if (pulseWidthUs >= CONTROL_SYNC_MIN_US && pulseWidthUs <= CONTROL_SYNC_MAX_US) {
    if (controlFrameActive && controlSymbolCount > 0) {
      finalizePendingControlFrame(controlSymbolCount);
    }
    controlFrameActive = true;
    controlSymbolCount = 0;
    return;
  }

  if (!controlFrameActive) {
    return;
  }

  const int symbol = decodeControlSymbol(pulseWidthUs);
  if (symbol < 0) {
    controlFrameActive = false;
    controlSymbolCount = 0;
    return;
  }

  if (controlSymbolCount < CONTROL_FRAME_SYMBOLS) {
    controlSymbols[controlSymbolCount++] = static_cast<uint8_t>(symbol);
  }

  if (controlSymbolCount < CONTROL_FRAME_SYMBOLS) {
    return;
  }

  finalizePendingControlFrame(controlSymbolCount);
  controlFrameActive = false;
  controlSymbolCount = 0;
}

bool controlLinkAlive() {
  uint32_t lastFrameMs = 0;
  noInterrupts();
  lastFrameMs = controlLastFrameMs;
  interrupts();
  return lastFrameMs != 0 && (millis() - lastFrameMs) <= CONTROL_LINK_TIMEOUT_MS;
}

void updateManagedOutputs() {
  uint8_t fanDuty = 0;
  uint8_t ledDuty = 0;
  uint32_t lastDataMs = 0;

  noInterrupts();
  fanDuty = controlFanDuty;
  ledDuty = controlLedDuty;
  lastDataMs = controlLastDataMs;
  interrupts();

  if (lastDataMs != 0 && (millis() - lastDataMs) <= CONTROL_LINK_TIMEOUT_MS) {
    analogWrite(FAN_PIN, dutyToPwm(fanDuty));
    analogWrite(STATUS_LED_PIN, dutyToPwm(ledDuty));
    return;
  }

  // 当前板子的风扇为高电平开启，因此链路未就绪时默认拉低保持关闭。
  analogWrite(FAN_PIN, 0);
  analogWrite(STATUS_LED_PIN, PWM_RANGE);
}

void startPowerKeyPulse() {
  pinMode(POWER_KEY_PIN, OUTPUT);
  digitalWrite(POWER_KEY_PIN, LOW);
  bootPressStartMs = millis();
  powerKeyReleased = false;
}

void setPowerKeyIdle() {
  pinMode(POWER_KEY_PIN, INPUT);
  powerKeyReleased = true;
}

void initOutputPins() {
  analogWriteRange(PWM_RANGE);
  analogWriteFreq(PWM_FREQ);
  setPowerKeyIdle();

  pinMode(STATUS_LED_PIN, OUTPUT);
  analogWrite(STATUS_LED_PIN, PWM_RANGE);

  pinMode(FAN_PIN, OUTPUT);
  analogWrite(FAN_PIN, 0);

  pinMode(PROBE_PIN_A, INPUT);
  pinMode(PROBE_PIN_B, INPUT);
}

void setStatusLed(bool on) {
  analogWrite(STATUS_LED_PIN, on ? STATUS_LED_ON_DUTY : 0);
}

void updateStatusLed() {
  if (WiFi.status() == WL_CONNECTED) {
    setStatusLed(true);
    return;
  }

  if (millis() - lastLedToggleMs >= 400) {
    lastLedToggleMs = millis();
    ledBlinkState = !ledBlinkState;
    setStatusLed(ledBlinkState);
  }
}

void updateProbeState() {
  const int currentA = digitalRead(PROBE_PIN_A);
  const int currentB = digitalRead(PROBE_PIN_B);
  const unsigned long now = millis();

  if (lastProbeA == -1) {
    lastProbeA = currentA;
    lastProbeB = currentB;
    lastProbeChangeMsA = now;
    lastProbeChangeMsB = now;
    return;
  }

  if (currentA != lastProbeA) {
    lastProbeA = currentA;
    ++probeChangeCountA;
    lastProbeChangeMsA = now;
  }

  if (currentB != lastProbeB) {
    lastProbeB = currentB;
    ++probeChangeCountB;
    lastProbeChangeMsB = now;
  }
}

void updatePowerKeyPulse() {
  if (powerKeyReleased) {
    return;
  }
  if (millis() - bootPressStartMs >= BOOT_HOLD_MS) {
    setPowerKeyIdle();
    Serial.println("POWER_KEY released -> high-Z");
  }
}

bool loadWifiConfig() {
  if (!LittleFS.exists(WIFI_CFG_PATH)) {
    return false;
  }
  File file = LittleFS.open(WIFI_CFG_PATH, "r");
  if (!file) {
    return false;
  }
  savedSsid = file.readStringUntil('\n');
  savedPass = file.readStringUntil('\n');
  savedSsid.trim();
  savedPass.trim();
  file.close();
  return savedSsid.length() > 0;
}

bool waitForSupervisorControlLink(unsigned long timeoutMs) {
  const unsigned long start = millis();
  while (millis() - start < timeoutMs) {
    updateProbeState();
    updateManagedOutputs();
    if (controlLinkAlive()) {
      return true;
    }
    delay(20);
  }
  return controlLinkAlive();
}

bool shouldPressPowerKeyOnBoot() {
  uint8_t reason = REASON_DEFAULT_RST;
  if (const rst_info* resetInfo = ESP.getResetInfoPtr()) {
    reason = resetInfo->reason;
  }
  bootResetReason = reason;
  bootSawSupervisorReply = false;
  bootPowerKeyPulseRequested = false;

  Serial.printf("Reset reason: %u\n", reason);
  Serial.print("Reset reason text: ");
  Serial.println(resetReasonName(reason));
  Serial.printf("Supervisor probe: wait control link %lu ms\n", SUPERVISOR_CONTROL_PROBE_MS);
  if (waitForSupervisorControlLink(SUPERVISOR_CONTROL_PROBE_MS)) {
    bootSawSupervisorReply = true;
    Serial.println("Supervisor probe: control link alive, skip power key pulse");
    return false;
  }

  if (!allowPowerKeyFallback(reason)) {
    Serial.println("Supervisor probe: no protocol reply after non-cold reset, keep power key idle");
    return false;
  }

  Serial.println("Supervisor probe: no protocol reply, allow cold-boot power key pulse");
  bootPowerKeyPulseRequested = true;
  return true;
}

bool saveWifiConfig(const String& ssid, const String& pass) {
  File file = LittleFS.open(WIFI_CFG_PATH, "w");
  if (!file) {
    return false;
  }
  file.println(ssid);
  file.println(pass);
  file.close();
  savedSsid = ssid;
  savedPass = pass;
  return true;
}

void clearWifiConfig() {
  LittleFS.remove(WIFI_CFG_PATH);
  savedSsid = "";
  savedPass = "";
}

void setupOta() {
  ArduinoOTA.setHostname(OTA_HOSTNAME);
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.onStart([]() { Serial.println("OTA start"); });
  ArduinoOTA.onEnd([]() { Serial.println("\nOTA end"); });
  ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
    Serial.printf("OTA progress: %u%%\r", (progress * 100U) / total);
  });
  ArduinoOTA.onError([](ota_error_t error) {
    Serial.printf("OTA error[%u]\n", static_cast<unsigned int>(error));
  });
  ArduinoOTA.begin();
  otaReady = true;
  Serial.printf("OTA ready: %s.local\n", OTA_HOSTNAME);
}

String scanNetworksHtml() {
  String html;
  html.reserve(2048);

  int count = WiFi.scanNetworks(false, true);
  if (count <= 0) {
    html += F("<div class='empty'>没扫到附近 Wi‑Fi，点“刷新列表”再试。</div>");
    WiFi.scanDelete();
    return html;
  }

  html += F("<div class='wifi-list'>");
  for (int i = 0; i < count; ++i) {
    String ssid = WiFi.SSID(i);
    int32_t rssi = WiFi.RSSI(i);
    bool open = (WiFi.encryptionType(i) == ENC_TYPE_NONE);
    html += F("<button type='button' class='wifi-item' onclick=\"pickSsid('");
    html += jsEscape(ssid);
    html += F("')\"><span>");
    html += htmlEscape(ssid);
    html += F("</span><small>");
    html += String(rssi);
    html += F(" dBm · ");
    html += open ? F("开放") : F("加密");
    html += F("</small></button>");
  }
  html += F("</div>");
  WiFi.scanDelete();
  return html;
}

String pageTemplate(const String& note) {
  String ssidValue = htmlEscape(savedSsid);
  String scanHtml = scanNetworksHtml();
  String page;
  page.reserve(5200);
  page += F(
      "<!doctype html><html><head><meta charset='utf-8'>"
      "<meta name='viewport' content='width=device-width,initial-scale=1'>"
      "<title>Mido Ctrl Setup</title>"
      "<style>"
      "body{font-family:Arial,'Microsoft YaHei',sans-serif;background:#edf3f7;margin:0;padding:24px;color:#1f2933;}"
      ".card{max-width:560px;margin:0 auto;background:#fff;border-radius:18px;padding:24px;box-shadow:0 14px 40px rgba(0,0,0,.08);}"
      "h1{margin:0 0 8px;font-size:28px;}p{color:#52606d;line-height:1.6;}"
      ".note{margin:16px 0;padding:14px 16px;background:#f4f8fb;border-radius:12px;}"
      "label{display:block;margin:18px 0 6px;font-weight:700;}"
      "input{width:100%;padding:14px 16px;border:1px solid #c9d6df;border-radius:12px;font-size:16px;box-sizing:border-box;}"
      "button{margin-top:18px;width:100%;padding:14px 16px;border:none;border-radius:12px;background:#0f8f65;color:#fff;font-size:16px;font-weight:700;}"
      ".wifi-list{display:grid;gap:10px;margin-top:16px;}"
      ".wifi-item{margin-top:0;display:flex;justify-content:space-between;align-items:center;background:#eef7f2;color:#163826;border:1px solid #c7e8d4;text-align:left;}"
      ".wifi-item small{color:#4c6b5a;font-size:13px;}"
      ".tools{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:16px;}"
      ".tools a,.tools button{margin-top:0;text-decoration:none;display:flex;align-items:center;justify-content:center;}"
      ".soft{background:#1f6feb;}"
      ".empty{padding:14px 16px;background:#f8fafc;border:1px dashed #c9d6df;border-radius:12px;color:#52606d;}"
      ".sub{margin-top:12px;font-size:14px;color:#7b8794;}"
      ".danger{display:inline-block;margin-top:16px;color:#b42318;text-decoration:none;}"
      "</style></head><body><div class='card'>");
  page += F("<h1>Mido 控制板配网</h1><p>先连热点 <strong>mido-ctrl-setup</strong>，再在这里填写路由 Wi‑Fi。连上后 OTA 才能用。</p>");
  page += F("<div class='note'>");
  page += htmlEscape(note);
  page += F("</div><div class='note'>"
            "探针输入状态：GPIO14=<span id='probe-a'>-</span> · RX(GPIO3)=<span id='probe-b'>-</span><br>"
            "变化计数：GPIO14=<span id='probe-a-count'>0</span> · RX(GPIO3)=<span id='probe-b-count'>0</span>"
            "</div><div class='tools'>"
            "<a href='/' class='soft'>刷新列表</a>"
            "<button type='button' onclick='location.reload()'>重新扫描</button>"
            "</div>"
            "<label>附近 Wi‑Fi</label>");
  page += scanHtml;
  page += F("<form method='post' action='/save'>"
            "<label>Wi‑Fi 名称</label><input name='ssid' maxlength='64' value='");
  page += ssidValue;
  page += F("'>"
            "<label>Wi‑Fi 密码</label><input name='password' type='password' maxlength='64' value=''>"
            "<button type='submit'>保存并连接</button></form>"
            "<div class='sub'>热点密码：12345678<br>OTA 主机名：mido-ctrl.local<br>OTA 密码：12345678</div>"
            "<a class='danger' href='/clear'>清除已保存 Wi‑Fi</a>"
            "<script>"
            "function pickSsid(ssid){document.querySelector('input[name=\"ssid\"]').value=ssid;document.querySelector('input[name=\"password\"]').focus();}"
            "async function refreshProbe(){"
            "try{"
            "const res=await fetch('/api/io');"
            "const data=await res.json();"
            "document.getElementById('probe-a').textContent=data.gpio14.level;"
            "document.getElementById('probe-b').textContent=data.rx.level;"
            "document.getElementById('probe-a-count').textContent=data.gpio14.changes;"
            "document.getElementById('probe-b-count').textContent=data.rx.changes;"
            "}catch(e){}"
            "}"
            "refreshProbe();setInterval(refreshProbe,500);"
            "</script>"
            "</div></body></html>");
  return page;
}

void handleIoStatus() {
  updateProbeState();
  uint8_t fanDuty = 0;
  uint8_t ledDuty = 0;
  uint32_t frameCount = 0;
  uint32_t helloFrameCount = 0;
  uint32_t dataFrameCount = 0;
  uint32_t lastFrameMs = 0;
  uint32_t lastDataMs = 0;
  uint8_t lastSeq = 0;
  uint8_t lastType = 0;
  bool protocolSynced = false;
  noInterrupts();
  fanDuty = controlFanDuty;
  ledDuty = controlLedDuty;
  frameCount = controlFrameCount;
  helloFrameCount = controlHelloFrameCount;
  dataFrameCount = controlDataFrameCount;
  lastFrameMs = controlLastFrameMs;
  lastDataMs = controlLastDataMs;
  lastSeq = controlLastSeq;
  lastType = controlLastType;
  protocolSynced = controlProtocolSynced;
  interrupts();

  String body;
  body.reserve(768);
  body += F("{\"ok\":true,\"gpio14\":{");
  body += F("\"pin\":14,\"level\":");
  body += String(lastProbeA < 0 ? digitalRead(PROBE_PIN_A) : lastProbeA);
  body += F(",\"changes\":");
  body += String(probeChangeCountA);
  body += F(",\"last_change_ms\":");
  body += String(lastProbeChangeMsA);
  body += F("},\"rx\":{");
  body += F("\"pin\":3,\"level\":");
  body += String(lastProbeB < 0 ? digitalRead(PROBE_PIN_B) : lastProbeB);
  body += F(",\"changes\":");
  body += String(probeChangeCountB);
  body += F(",\"last_change_ms\":");
  body += String(lastProbeChangeMsB);
  body += F("},\"control\":{");
  body += F("\"fan_duty\":");
  body += String(fanDuty);
  body += F(",\"led_duty\":");
  body += String(ledDuty);
  body += F(",\"frames\":");
  body += String(frameCount);
  body += F(",\"last_frame_ms\":");
  body += String(lastFrameMs);
  body += F(",\"last_data_ms\":");
  body += String(lastDataMs);
  body += F(",\"link_alive\":");
  body += controlLinkAlive() ? F("true") : F("false");
  body += F("},\"boot\":{");
  body += F("\"reset_reason\":");
  body += String(bootResetReason);
  body += F(",\"reset_reason_text\":\"");
  body += resetReasonName(bootResetReason);
  body += F("\",\"supervisor_reply\":");
  body += bootSawSupervisorReply ? F("true") : F("false");
  body += F(",\"power_key_pulsed\":");
  body += bootPowerKeyPulseRequested ? F("true") : F("false");
  body += F("},\"protocol\":{");
  body += F("\"magic\":");
  body += String(CONTROL_PROTOCOL_MAGIC);
  body += F(",\"magic_hex\":\"0x");
  body += String(CONTROL_PROTOCOL_MAGIC, HEX);
  body += F("\",\"version\":");
  body += String(CONTROL_PROTOCOL_VERSION);
  body += F(",\"synced\":");
  body += (protocolSynced && controlLinkAlive()) ? F("true") : F("false");
  body += F(",\"last_seq\":");
  body += String(lastSeq);
  body += F(",\"last_type\":\"");
  body += frameTypeName(lastType);
  body += F("\",\"frames\":");
  body += String(frameCount);
  body += F(",\"hello_frames\":");
  body += String(helloFrameCount);
  body += F(",\"data_frames\":");
  body += String(dataFrameCount);
  body += F("}}");
  server.send(200, "application/json; charset=utf-8", body);
}

bool connectStation(unsigned long timeoutMs) {
  if (savedSsid.isEmpty()) {
    return false;
  }

  WiFi.mode(WIFI_AP_STA);
  WiFi.begin(savedSsid.c_str(), savedPass.c_str());
  Serial.printf("Connecting WiFi: %s\n", savedSsid.c_str());

  unsigned long start = millis();
  while (millis() - start < timeoutMs) {
    updatePowerKeyPulse();
    delay(100);
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("WiFi connected, IP=%s\n", WiFi.localIP().toString().c_str());
      if (!otaReady) {
        setupOta();
      }
      return true;
    }
    if (millis() - lastWifiLogMs >= 1000) {
      lastWifiLogMs = millis();
      Serial.print(".");
    }
  }
  Serial.println();
  Serial.println("WiFi connect timeout, keep AP mode");
  return false;
}

void handleRoot() {
  String note = WiFi.status() == WL_CONNECTED
                    ? String("当前已连上路由，IP：") + WiFi.localIP().toString()
                    : String("当前未连上路由，仍在热点配网模式。AP IP：") + WiFi.softAPIP().toString();
  server.send(200, "text/html; charset=utf-8", pageTemplate(note));
}

void handleSave() {
  String ssid = server.arg("ssid");
  String password = server.arg("password");
  ssid.trim();
  password.trim();

  if (ssid.isEmpty()) {
    server.send(400, "text/html; charset=utf-8", pageTemplate("Wi‑Fi 名称不能为空"));
    return;
  }
  if (!saveWifiConfig(ssid, password)) {
    server.send(500, "text/html; charset=utf-8", pageTemplate("保存失败，LittleFS 写入错误"));
    return;
  }

  bool ok = connectStation(20000);
  String note = ok ? String("连接成功，当前 IP：") + WiFi.localIP().toString()
                   : String("已保存，但连接失败，请检查密码。热点仍可继续使用，AP IP：") + WiFi.softAPIP().toString();
  server.send(200, "text/html; charset=utf-8", pageTemplate(note));
}

void handleClear() {
  clearWifiConfig();
  WiFi.disconnect();
  server.send(200, "text/html; charset=utf-8", pageTemplate("已清除保存的 Wi‑Fi 配置"));
}

void setupPortal() {
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(AP_SSID, AP_PASS);
  Serial.printf("AP ready: %s / %s, IP=%s\n", AP_SSID, AP_PASS, WiFi.softAPIP().toString().c_str());

  server.on("/", HTTP_GET, handleRoot);
  server.on("/api/io", HTTP_GET, handleIoStatus);
  server.on("/save", HTTP_POST, handleSave);
  server.on("/clear", HTTP_GET, handleClear);
  server.begin();
}

void setup() {
  initOutputPins();

  Serial.begin(SERIAL_BAUD);
  Serial.println();
  Serial.println("ESP8266 power-key boot helper with AP portal");
  Serial.println("POWER_KEY idle HIGH, probe supervisor before pulse");

  LittleFS.begin();
  loadWifiConfig();
  setupPortal();
  attachInterrupt(digitalPinToInterrupt(PROBE_PIN_A), handleControlWireEdge, CHANGE);

  if (shouldPressPowerKeyOnBoot()) {
    startPowerKeyPulse();
    Serial.println("POWER_KEY forced LOW for 5s, then high-Z");
  }

  if (savedSsid.length() && WiFi.status() != WL_CONNECTED) {
    connectStation(20000);
  }
}

void loop() {
  updatePowerKeyPulse();
  updateProbeState();
  updateManagedOutputs();
  server.handleClient();
  if (otaReady) {
    ArduinoOTA.handle();
  }
}
