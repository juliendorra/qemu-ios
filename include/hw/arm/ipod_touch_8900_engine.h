#ifndef HW_ARM_IPOD_TOUCH_8900_ENGINE_H
#define HW_ARM_IPOD_TOUCH_8900_ENGINE_H

#include "qemu/osdep.h"
#include "qemu-common.h"
#include "hw/hw.h"
#include "exec/hwaddr.h"
#include "exec/memory.h"
#include <openssl/aes.h>

typedef struct {
	unsigned char magic[ 4 ];
	unsigned char version[ 3 ];
	unsigned char encrypted;
	unsigned char unknownNull[4];
	unsigned int sizeOfData;
	unsigned int footerSignatureOffset;
	unsigned int footerCertOffset;
	unsigned int footerCertLen;
	unsigned char key1[ 32 ];
	unsigned char unknownVersion[ 4 ];
	unsigned char key2[ 16 ];
	unsigned char padding[ 1968 ];
} header8900;

static uint64_t s5l8900_8900_engine_read(void *opaque, hwaddr offset, unsigned size)
{
    return 0;
}

static void convert_hex_key_to_bin(const char *str, uint8_t *bytes, int maxlen)
{
	int bytelen = maxlen;
	int rpos, wpos = 0;

	for(rpos = 0; rpos < bytelen; rpos++) {
		sscanf(&str[rpos*2], "%02hhx", &bytes[wpos++]);
	}
}

static void s5l8900_8900_engine_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
	AES_KEY aes_decrypt_key;
	uint8_t aes_key[16];

	const char ramdiskKey[] = "188458A6D15034DFE386F23B61D43774";
	unsigned char iv[AES_BLOCK_SIZE];
	size_t data_current, data_len;

	if(offset != 0x0) { return; }

	printf("Reading 8900 header with length %zu at address 0x%08" HWADDR_PRIx "\n",
	       sizeof(header8900), value);

    AddressSpace *nsas = (AddressSpace *)opaque;
    header8900 *header = malloc(sizeof(header8900));
    address_space_rw(nsas, value, MEMTXATTRS_UNSPECIFIED, (uint8_t *)header, sizeof(header8900), 0);

	if (memcmp(header->magic, "8900", sizeof(header->magic)) != 0)
	{
		printf("Bad 8900 magic\n");
		free(header);
		return;
	}

	data_len = header->sizeOfData;
	if (header->encrypted != 0x03 && header->encrypted != 0x04) {
		printf("Unsupported 8900 encryption marker 0x%02x\n",
		       header->encrypted);
		free(header);
		return;
	}
	if (header->encrypted == 0x04) {
		/* The payload is already plaintext. The previous assignment-in-condition
		 * mutated this marker and then decrypted it anyway. */
		free(header);
		return;
	}
	if (data_len % AES_BLOCK_SIZE != 0) {
		printf("Invalid 8900 encrypted length %zu\n", data_len);
		free(header);
		return;
	}

	printf("Will decrypt 8900 image at address 0x%08" HWADDR_PRIx
	       " (len: %zu bytes)\n", value, data_len);

	// read the data into a buffer
	uint8_t *inbuf = (uint8_t *)malloc(data_len);
	address_space_rw(nsas, value + sizeof(header8900), MEMTXATTRS_UNSPECIFIED, (uint8_t *)inbuf, data_len, 0);
	uint8_t *outbuf = (uint8_t *)malloc(data_len);
	uint8_t *intbuf = (uint8_t *)malloc(AES_BLOCK_SIZE);

	convert_hex_key_to_bin(ramdiskKey, aes_key, 16);
	AES_set_decrypt_key(aes_key, 128, &aes_decrypt_key);
	memset(iv, 0, AES_BLOCK_SIZE);

	data_current = 0;
	while(data_current < data_len)
	{
		AES_cbc_encrypt(inbuf + data_current, intbuf, AES_BLOCK_SIZE, &aes_decrypt_key, iv, 0);
		memcpy(outbuf + data_current, intbuf, AES_BLOCK_SIZE);
		data_current = data_current + AES_BLOCK_SIZE;
	}

	// write the decrypted output buffer
	address_space_rw(nsas, value + sizeof(header8900), MEMTXATTRS_UNSPECIFIED, (uint8_t *)outbuf, data_len, 1);

	free(header);
	free(inbuf);
	free(outbuf);
	free(intbuf);
}

static const MemoryRegionOps engine_8900_ops = {
    .read = s5l8900_8900_engine_read,
    .write = s5l8900_8900_engine_write,
    .endianness = DEVICE_NATIVE_ENDIAN,
};

#endif
