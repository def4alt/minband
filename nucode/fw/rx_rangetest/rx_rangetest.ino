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

#define RANGETEST_PAYLOAD_SIZE 40 // known-good default, see handoff plan
#define RANGETEST_PHY BLE_GAP_PHY_CODED // BLE_GAP_PHY_1MBPS / _2MBPS / _CODED
// Stop-and-wait: don't generate frame N+1 until frame N's echo is fully back
// (or this times out). Without it, on Coded PHY a multi-packet frame's RTT
// can exceed the send interval, so RX starts frame N+1 while TX's echo of
// frame N is still mid-transit - tx_echo.ino has no frame-boundary
// awareness, so it splices the two frames' bytes together and bridge.py's
// CRC check correctly rejects the result. See handoff plan section 2.
#define RANGETEST_ECHO_TIMEOUT_MS 2000

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

// Set once frame N is fully sent; cleared by pumpBleToUsb() once its echo
// (both SLIP_END bytes) has come back, or here on timeout as a fallback so
// a lost echo can't wedge the generator forever.
static bool waitingEcho = false;
static unsigned long echoWaitStartMs = 0;

static void pumpGeneratedToBle() {
  if (connHandle == BLE_CONN_HANDLE_INVALID) return;

  if (waitingEcho) {
    if (millis() - echoWaitStartMs < RANGETEST_ECHO_TIMEOUT_MS) return;
    waitingEcho = false; // echo never came back; don't stall forever
  }

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
  waitingEcho = true;
  echoWaitStartMs = millis();
}

// Forwards TX's echoed bytes to USB serial for bridge.py to decode, same
// as rx_peripheral.ino's pumpBleToUsb(). Also watches for the echoed
// frame's two SLIP_END bytes (frame start + end marker) to know the
// in-flight frame is fully back, clearing waitingEcho so the next frame
// can go out.
static int echoEndsSeen = 0;

static void pumpBleToUsb() {
  uint8_t buf[256];
  int n = bleuart.available();
  if (n <= 0) return;
  if (n > (int)sizeof(buf)) n = sizeof(buf);
  int got = bleuart.read(buf, n);
  if (got <= 0) return;
  Serial.write(buf, got);

  if (waitingEcho) {
    for (int i = 0; i < got; i++) {
      if (buf[i] == SLIP_END) {
        echoEndsSeen++;
        if (echoEndsSeen >= 2) {
          waitingEcho = false;
          echoEndsSeen = 0;
          break;
        }
      }
    }
  } else {
    echoEndsSeen = 0;
  }
}

void loop() {
  pumpGeneratedToBle();
  pumpBleToUsb();
}
