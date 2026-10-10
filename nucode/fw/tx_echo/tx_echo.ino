// MinBand BLE range test - TX (central) side, generic echo variant.
// One-time flash, config-agnostic: whatever it receives over BLE, it
// immediately writes back. No USB needed once connected (battery only).
// Does not request a PHY itself - RX (rx_rangetest.ino) drives PHY choice
// from its own connect callback, so only RX needs reflashing between test
// configs (payload size, PHY). Scan/connect logic is unchanged from
// tx_central.ino.
#include <bluefruit.h>

BLEClientUart clientUart;

static uint16_t connHandle = BLE_CONN_HANDLE_INVALID;

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

// Same MTU-aware chunking as tx_central.ino's pumpUsbToBle() - clientUart
// write() does not auto-chunk, a call bigger than MTU-3 silently truncates.
static uint8_t echoBuf[2600];
static size_t echoLen = 0;
static size_t echoSent = 0;

void loop() {
  if (connHandle == BLE_CONN_HANDLE_INVALID) {
    delay(50);
    return;
  }

  if (echoLen == 0) {
    int n = clientUart.available();
    if (n > 0) {
      if (n > (int)sizeof(echoBuf)) n = sizeof(echoBuf);
      int got = clientUart.read(echoBuf, n);
      if (got > 0) {
        echoLen = (size_t)got;
        echoSent = 0;
        digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));
      }
    }
    if (echoLen == 0) return;
  }

  uint16_t mtu = Bluefruit.Connection(connHandle)->getMtu();
  size_t chunk = mtu > 3 ? (size_t)(mtu - 3) : 20;

  while (echoSent < echoLen) {
    size_t want = (echoLen - echoSent) < chunk ? (echoLen - echoSent) : chunk;
    size_t wrote = clientUart.write(echoBuf + echoSent, want);
    if (wrote == 0) return; // BLE link busy; retry next loop
    echoSent += wrote;
  }
  echoLen = 0;
}
