#include "MinBandFrame.h"

uint16_t crc16_ccitt(const uint8_t* data, size_t len, uint16_t crc) {
  for (size_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (uint8_t b = 0; b < 8; b++) {
      crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : (crc << 1);
    }
  }
  return crc;
}

static bool emit(uint8_t b, uint8_t* out, size_t outCap, size_t* pos) {
  if (b == SLIP_END) {
    if (*pos + 2 > outCap) return false;
    out[(*pos)++] = SLIP_ESC;
    out[(*pos)++] = SLIP_ESC_END;
  } else if (b == SLIP_ESC) {
    if (*pos + 2 > outCap) return false;
    out[(*pos)++] = SLIP_ESC;
    out[(*pos)++] = SLIP_ESC_ESC;
  } else {
    if (*pos + 1 > outCap) return false;
    out[(*pos)++] = b;
  }
  return true;
}

size_t minband_slip_encode(uint8_t type, const uint8_t* payload, size_t len,
                            uint8_t* out, size_t outCap) {
  size_t pos = 0;
  if (pos + 1 > outCap) return 0;
  out[pos++] = SLIP_END;

  if (!emit(type, out, outCap, &pos)) return 0;
  for (size_t i = 0; i < len; i++) {
    if (!emit(payload[i], out, outCap, &pos)) return 0;
  }

  uint16_t crc = crc16_ccitt(&type, 1);
  crc = crc16_ccitt(payload, len, crc);
  if (!emit((uint8_t)(crc & 0xFF), out, outCap, &pos)) return 0;
  if (!emit((uint8_t)(crc >> 8), out, outCap, &pos)) return 0;

  if (pos + 1 > outCap) return 0;
  out[pos++] = SLIP_END;
  return pos;
}
