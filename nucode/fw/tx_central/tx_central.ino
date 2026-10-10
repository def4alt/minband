// MinBand BLE serial pipe - TX (central) side.
// Byte-transparent USB<->BLE bridge. Knows nothing about the MinBand
// wire protocol; bridge.py on the PC handles SLIP+CRC framing.
#include <bluefruit.h>
#include <MinBandFrame.h>

BLEClientUart clientUart;

static uint16_t connHandle = BLE_CONN_HANDLE_INVALID;

// Step 4's telemetry injector needs to know it's at a frame boundary
// before splicing in a 0x02 packet. Tracked here even though unused
// until then, per spec.
static bool lastUsbByteWasFrameEnd = true;

static void scanCallback(ble_gap_evt_adv_report_t* report) {
  uint8_t nameBuf[32];
  uint8_t nameLen = Bluefruit.Scanner.parseReportByType(
      report, BLE_GAP_AD_TYPE_COMPLETE_LOCAL_NAME, nameBuf, sizeof(nameBuf) - 1);

  // UUID filter alone isn't enough - any bleuart peripheral in the room
  // would match. Require the exact advertised name too.
  bool nameMatches = false;
  if (nameLen > 0) {
    nameBuf[nameLen] = '\0';
    nameMatches = strcmp((const char*)nameBuf, "MinBand-RX") == 0;
  }

  if (nameMatches) {
    Bluefruit.Central.connect(report);
  } else {
    Bluefruit.Scanner.resume();
  }
}

static void onConnect(uint16_t conn_handle) {
  if (!clientUart.discover(conn_handle)) {
    Bluefruit.disconnect(conn_handle);
    return;
  }
  clientUart.enableTXD();

  connHandle = conn_handle;
  digitalWrite(LED_CONN, HIGH);

  BLEConnection* conn = Bluefruit.Connection(conn_handle);
  conn->requestPHY(BLE_GAP_PHY_2MBPS);
  conn->requestDataLengthUpdate();
  conn->requestMtuExchange(247);
}

static void onDisconnect(uint16_t conn_handle, uint8_t reason) {
  (void) reason;
  if (conn_handle == connHandle) {
    connHandle = BLE_CONN_HANDLE_INVALID;
    digitalWrite(LED_CONN, LOW);
  }
}

void setup() {
  Serial.begin(1000000);
  pinMode(LED_CONN, OUTPUT);
  digitalWrite(LED_CONN, LOW);

  // All configXxx() calls must precede Bluefruit.begin().
  Bluefruit.configCentralBandwidth(BANDWIDTH_MAX);
  Bluefruit.begin(0, 1);
  Bluefruit.setName("MinBand-TX");

  clientUart.begin();

  Bluefruit.Central.setConnectCallback(onConnect);
  Bluefruit.Central.setDisconnectCallback(onDisconnect);

  // No Scanner-level UUID filter: the bleuart service UUID is only in the
  // ADV_IND packet while the name is in the SCAN_RSP packet (RX has no room
  // for both in one 31-byte adv packet). filterUuid() would silently drop
  // the SCAN_RSP report before scanCallback ever sees the name. Matching on
  // the exact advertised name alone is unique enough.
  Bluefruit.Scanner.setRxCallback(scanCallback);
  Bluefruit.Scanner.restartOnDisconnect(true);
  Bluefruit.Scanner.useActiveScan(true);
  Bluefruit.Scanner.setInterval(160, 80); // 100 ms / 80 ms, in 0.625 ms units
  Bluefruit.Scanner.start(0);
}

static void pumpUsbToBle() {
  static uint8_t buf[1536];
  static size_t len = 0;
  static size_t sent = 0;

  if (len == 0) {
    while (Serial.available() && len < sizeof(buf)) buf[len++] = Serial.read();
    sent = 0;
  }
  if (len == 0 || connHandle == BLE_CONN_HANDLE_INVALID) {
    len = 0;
    sent = 0;
    return;
  }

  uint16_t mtu = Bluefruit.Connection(connHandle)->getMtu();
  size_t chunk = mtu > 3 ? (size_t)(mtu - 3) : 20;

  while (sent < len) {
    size_t want = (len - sent) < chunk ? (len - sent) : chunk;
    size_t wrote = clientUart.write(buf + sent, want);
    if (wrote == 0) break; // BLE link busy; keep remainder, retry next loop
    sent += wrote;
  }
  if (sent >= len) {
    len = 0;
    sent = 0;
  }
}

static void pumpBleToUsb() {
  uint8_t buf[256];
  int n = clientUart.available();
  if (n <= 0) return;
  if (n > (int)sizeof(buf)) n = sizeof(buf);
  int got = clientUart.read(buf, n);
  if (got <= 0) return;
  Serial.write(buf, got);
  lastUsbByteWasFrameEnd = (buf[got - 1] == SLIP_END);
}

void loop() {
  pumpUsbToBle();
  pumpBleToUsb();
}
