// MinBand BLE serial pipe - RX (peripheral) side.
// Byte-transparent USB<->BLE bridge. Knows nothing about the MinBand
// wire protocol; bridge.py on the PC handles SLIP+CRC framing.
#include <bluefruit.h>
#include <MinBandFrame.h>

BLEUart bleuart;

static uint16_t connHandle = BLE_CONN_HANDLE_INVALID;

// Step 4's telemetry injector needs to know it's at a frame boundary
// before splicing in a 0x02 packet. Tracked here even though unused
// until then, per spec.
static bool lastUsbByteWasFrameEnd = true;

static void onConnect(uint16_t conn_handle) {
  connHandle = conn_handle;
  BLEConnection* conn = Bluefruit.Connection(conn_handle);
  conn->requestPHY(BLE_GAP_PHY_2MBPS);
  conn->requestDataLengthUpdate();
  conn->requestMtuExchange(247);
}

static void onDisconnect(uint16_t conn_handle, uint8_t reason) {
  (void) reason;
  if (conn_handle == connHandle) connHandle = BLE_CONN_HANDLE_INVALID;
}

static void startAdv(void) {
  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addTxPower();
  Bluefruit.Advertising.addService(bleuart);
  Bluefruit.ScanResponse.addName();
  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setInterval(32, 244); // 20 ms fast, 152.5 ms slow
  Bluefruit.Advertising.setFastTimeout(30);
  Bluefruit.Advertising.start(0);
}

void setup() {
  Serial.begin(1000000);

  // All configXxx() calls must precede Bluefruit.begin().
  Bluefruit.configPrphBandwidth(BANDWIDTH_MAX);
  Bluefruit.begin(1, 0);
  Bluefruit.setTxPower(8);
  Bluefruit.setName("MinBand-RX");
  Bluefruit.Periph.setConnectCallback(onConnect);
  Bluefruit.Periph.setDisconnectCallback(onDisconnect);

  bleuart.begin();

  startAdv();
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
    size_t wrote = bleuart.write(buf + sent, want);
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
  int n = bleuart.available();
  if (n <= 0) return;
  if (n > (int)sizeof(buf)) n = sizeof(buf);
  int got = bleuart.read(buf, n);
  if (got <= 0) return;
  Serial.write(buf, got);
  lastUsbByteWasFrameEnd = (buf[got - 1] == SLIP_END);
}

void loop() {
  pumpUsbToBle();
  pumpBleToUsb();
}
