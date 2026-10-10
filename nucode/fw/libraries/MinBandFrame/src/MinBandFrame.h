#ifndef MINBAND_FRAME_H_
#define MINBAND_FRAME_H_

#include <Arduino.h>

#define SLIP_END  0xC0
#define SLIP_ESC  0xDB
#define SLIP_ESC_END 0xDC
#define SLIP_ESC_ESC 0xDD

#define MINBAND_FRAME_TYPE_DATA      0x01
#define MINBAND_FRAME_TYPE_TELEMETRY 0x02

uint16_t crc16_ccitt(const uint8_t* data, size_t len, uint16_t crc = 0xFFFF);

// Encodes `END SLIP(type|payload|crc16) END` into out. Returns bytes written,
// or 0 if it would not fit in outCap.
size_t minband_slip_encode(uint8_t type, const uint8_t* payload, size_t len,
                            uint8_t* out, size_t outCap);

#endif // MINBAND_FRAME_H_
