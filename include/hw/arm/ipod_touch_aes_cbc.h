#ifndef HW_ARM_IPOD_TOUCH_AES_CBC_H
#define HW_ARM_IPOD_TOUCH_AES_CBC_H

#include "qemu/osdep.h"
#include "crypto/aes.h"

/*
 * CBC chaining over QEMU's in-tree AES, replacing OpenSSL's AES_cbc_encrypt.
 *
 * QEMU ships AES_set_{encrypt,decrypt}_key and AES_{encrypt,decrypt} with the
 * same signatures as OpenSSL's legacy API -- crypto/aes.h renames them
 * precisely so the two cannot collide -- but no CBC wrapper. OpenSSL does not
 * cross-compile to wasm64 and the browser port needs the AES and 8900 engines,
 * so the chaining lives here instead. Dropping the dependency also removes the
 * only reason the native build needed -lcrypto.
 *
 * This mirrors OpenSSL's CRYPTO_cbc128_{encrypt,decrypt} exactly, INCLUDING
 * the trailing partial block (encrypt pads from the IV, decrypt truncates) and
 * the in-place update of ivec, so the guest sees identical bytes to before.
 *
 * Both callers only ever expand a DECRYPT key schedule. Passing enc != 0 with
 * such a schedule produced garbage under OpenSSL and still does: that path is
 * reproduced rather than "fixed", because nothing in the firmware takes it and
 * changing it would be an unverifiable behaviour change.
 */
static inline void it_aes_cbc(const uint8_t *in, uint8_t *out, size_t len,
                              const AES_KEY *key, uint8_t *ivec, int enc)
{
    uint8_t iv[AES_BLOCK_SIZE];
    uint8_t tmp[AES_BLOCK_SIZE];
    size_t n;

    memcpy(iv, ivec, AES_BLOCK_SIZE);

    if (enc) {
        while (len >= AES_BLOCK_SIZE) {
            for (n = 0; n < AES_BLOCK_SIZE; n++) {
                out[n] = in[n] ^ iv[n];
            }
            AES_encrypt(out, out, key);
            memcpy(iv, out, AES_BLOCK_SIZE);
            in += AES_BLOCK_SIZE;
            out += AES_BLOCK_SIZE;
            len -= AES_BLOCK_SIZE;
        }
        if (len) {
            for (n = 0; n < len; n++) {
                out[n] = in[n] ^ iv[n];
            }
            for (n = len; n < AES_BLOCK_SIZE; n++) {
                out[n] = iv[n];
            }
            AES_encrypt(out, out, key);
            memcpy(iv, out, AES_BLOCK_SIZE);
        }
    } else {
        while (len >= AES_BLOCK_SIZE) {
            uint8_t cipher[AES_BLOCK_SIZE];

            memcpy(cipher, in, AES_BLOCK_SIZE);   /* in may alias out */
            AES_decrypt(in, tmp, key);
            for (n = 0; n < AES_BLOCK_SIZE; n++) {
                out[n] = tmp[n] ^ iv[n];
            }
            memcpy(iv, cipher, AES_BLOCK_SIZE);
            in += AES_BLOCK_SIZE;
            out += AES_BLOCK_SIZE;
            len -= AES_BLOCK_SIZE;
        }
        if (len) {
            uint8_t cipher[AES_BLOCK_SIZE];

            memcpy(cipher, in, AES_BLOCK_SIZE);
            AES_decrypt(in, tmp, key);
            for (n = 0; n < len; n++) {
                out[n] = tmp[n] ^ iv[n];
            }
            memcpy(iv, cipher, AES_BLOCK_SIZE);
        }
    }

    memcpy(ivec, iv, AES_BLOCK_SIZE);
}

#endif /* HW_ARM_IPOD_TOUCH_AES_CBC_H */
