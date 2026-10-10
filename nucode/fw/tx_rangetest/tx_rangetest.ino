// MinBand BLE range test - TX (central) side, battery-only variant.
// Self-generates MinBand-framed data instead of piping USB serial, so it
// keeps transmitting after being unplugged from the PC. Blinks LED_BUILTIN
// on every send so link activity stays visible while carried out of range.
// RX (rx_peripheral.ino) is unchanged: it still forwards raw bytes to its
// own USB serial, where the existing bridge.py + /telemetry on that end
// decodes these frames and reports crc_errors/seq_lost as usual.
#include <bluefruit.h>
#include <MinBandFrame.h>

BLEClientUart clientUart;

static uint16_t connHandle = BLE_CONN_HANDLE_INVALID;
static uint16_t seq = 0;

#define RANGETEST_PAYLOAD_SIZE 1200
#define RANGETEST_SEND_INTERVAL_MS 100

static void scanCallback(ble_gap_evt_adv_report_t* report) {
  uint8_t nameBuf[32];
  uint8_t nameLen = Bluefruit.Scanner.parseReportByType(
      report, BLE_GAP_AD_TYPE_COMPLETE_LOCAL_NAME, nameBuf, sizeof(nameBuf) - 1);

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
  conn->requestPHY(BLE_GAP_PHY_CODED);
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
  pinMode(LED_CONN, OUTPUT);
  digitalWrite(LED_CONN, LOW);
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  Bluefruit.configCentralBandwidth(BANDWIDTH_MAX);
  Bluefruit.begin(0, 1);
  Bluefruit.setName("MinBand-TX");

  clientUart.begin();

  Bluefruit.Central.setConnectCallback(onConnect);
  Bluefruit.Central.setDisconnectCallback(onDisconnect);

  Bluefruit.Scanner.setRxCallback(scanCallback);
  Bluefruit.Scanner.restartOnDisconnect(true);
  Bluefruit.Scanner.useActiveScan(true);
  Bluefruit.Scanner.setInterval(160, 80); // 100 ms / 80 ms, in 0.625 ms units
  Bluefruit.Scanner.start(0);
}

// clientUart.write() does not auto-chunk to the connection MTU (same
// reason tx_central.ino's pumpUsbToBle() chunks manually) - a single call
// with a buffer bigger than MTU-3 silently writes less than requested and
// drops the rest, mangling the frame. Static buffers avoid growing the
// stack with large arrays every loop() call.
static uint8_t frameBuf[2600]; // worst-case SLIP escaping of a 1200B+2B-seq body
static size_t frameLen = 0;
static size_t frameSent = 0;

void loop() {
  if (connHandle == BLE_CONN_HANDLE_INVALID) {
    delay(50);
    return;
  }

  if (frameLen == 0) {
    // body = seq:u16 LE + filler, matches bridge.py's TYPE_DATA wire format
    // exactly so the unmodified RX-side bridge.py decodes/counts it as usual.
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
    size_t wrote = clientUart.write(frameBuf + frameSent, want);
    if (wrote == 0) return; // BLE link busy; retry this same frame next loop
    frameSent += wrote;
  }

  seq++;
  digitalWrite(LED_BUILTIN, (seq % 2) == 0 ? HIGH : LOW);
  frameLen = 0;
  delay(RANGETEST_SEND_INTERVAL_MS);
}
