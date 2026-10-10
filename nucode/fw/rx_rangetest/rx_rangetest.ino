// MinBand BLE range test - RX (peripheral) side, emitter variant.
// Generates MinBand-framed data itself (instead of piping USB serial),
// sends it to TX (tx_echo.ino), and forwards whatever TX echoes back to
// USB serial. The existing bridge.py + /telemetry on this end decodes
// those echoed frames and reports crc_errors/seq_lost as usual - now
// measuring round-trip survival instead of one-way.
// Payload size and PHY are the two knobs for this test; only this board
// needs reflashing to change them, TX's echo firmware never changes.
#include <bluefruit.h>
#include <MinBandFrame.h>

BLEUart bleuart;

static uint16_t connHandle = BLE_CONN_HANDLE_INVALID;
static uint16_t seq = 0;

#define RANGETEST_PAYLOAD_SIZE 40 // only size confirmed clean round-trip so far, see handoff plan
#define RANGETEST_SEND_INTERVAL_MS 100
#define RANGETEST_PHY BLE_GAP_PHY_CODED // BLE_GAP_PHY_1MBPS / _2MBPS / _CODED

static void onConnect(uint16_t conn_handle) {
  connHandle = conn_handle;
  BLEConnection* conn = Bluefruit.Connection(conn_handle);
  conn->requestPHY(RANGETEST_PHY);
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

  Bluefruit.configPrphBandwidth(BANDWIDTH_MAX);
  Bluefruit.begin(1, 0);
  Bluefruit.setTxPower(8);
  Bluefruit.setName("MinBand-RX");
  Bluefruit.Periph.setConnectCallback(onConnect);
  Bluefruit.Periph.setDisconnectCallback(onDisconnect);

  bleuart.begin();

  startAdv();
}

// Same MTU-aware chunking as rx_peripheral.ino's pumpUsbToBle() - bleuart
// write() does not auto-chunk, a call bigger than MTU-3 silently truncates.
static uint8_t frameBuf[2600]; // worst-case SLIP escaping of a 1200B+2B-seq body
static size_t frameLen = 0;
static size_t frameSent = 0;

static void pumpGeneratedToBle() {
  if (connHandle == BLE_CONN_HANDLE_INVALID) return;

  if (frameLen == 0) {
    static uint8_t body[2 + RANGETEST_PAYLOAD_SIZE];
    body[0] = (uint8_t)(seq & 0xFF);
    body[1] = (uint8_t)(seq >> 8);
    for (int i = 0; i < RANGETEST_PAYLOAD_SIZE; i++) {
      body[2 + i] = (uint8_t)('A' + (i % 26));
    }
    frameLen = minband_slip_encode(MINBAND_FRAME_TYPE_DATA, body, sizeof(body),
                                    frameBuf, sizeof(frameBuf));
    frameSent = 0;
    if (frameLen == 0) return; // shouldn't happen, buffer sized for worst case
  }

  uint16_t mtu = Bluefruit.Connection(connHandle)->getMtu();
  size_t chunk = mtu > 3 ? (size_t)(mtu - 3) : 20;

  while (frameSent < frameLen) {
    size_t want = (frameLen - frameSent) < chunk ? (frameLen - frameSent) : chunk;
    size_t wrote = bleuart.write(frameBuf + frameSent, want);
    if (wrote == 0) return; // BLE link busy; retry this same frame next loop
    frameSent += wrote;
  }

  seq++;
  frameLen = 0;
  delay(RANGETEST_SEND_INTERVAL_MS);
}

// Forwards TX's echoed bytes to USB serial for bridge.py to decode, same
// as rx_peripheral.ino's pumpBleToUsb().
static void pumpBleToUsb() {
  uint8_t buf[256];
  int n = bleuart.available();
  if (n <= 0) return;
  if (n > (int)sizeof(buf)) n = sizeof(buf);
  int got = bleuart.read(buf, n);
  if (got <= 0) return;
  Serial.write(buf, got);
}

void loop() {
  pumpGeneratedToBle();
  pumpBleToUsb();
}
