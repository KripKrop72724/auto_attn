/* SPDX-FileCopyrightText: 2024-2026 Espressif Systems (Shanghai) CO LTD
 * SPDX-License-Identifier: Apache-2.0
 * Exact function from ESP-IDF commit 2c211b236707889e8400c4dc5644dd5c4ee071e0.
 * components/mbedtls/port/aes/dma/esp_aes_dma_core.c
 * Complete SDK source SHA256 is pinned by patch_idf_aes.py; this function is
 * independently checked against that source whenever IDF_PATH is available.
 */
static int esp_aes_process_dma_ext_ram(esp_aes_context *ctx, const unsigned char *input, unsigned char *output, size_t len, uint8_t *stream_out, bool realloc_input, bool realloc_output)
{
    size_t chunk_len;
    int ret = 0;
    int offset = 0;
    uint32_t input_heap_caps = MALLOC_CAP_DMA;
    uint32_t output_heap_caps = MALLOC_CAP_DMA;
    unsigned char *input_buf = NULL;
    unsigned char *output_buf = NULL;
    const unsigned char *dma_input;
    chunk_len = MIN(AES_MAX_CHUNK_WRITE_SIZE, len);

    size_t input_alignment = 1;
    size_t output_alignment = 1;

/* When AES-DMA operations are carried out using external memory with external memory encryption enabled,
   we need to make sure that the addresses and the sizes of the buffers on which the DMA operates are 16 byte-aligned.
   This is only applicable for ESP32-P4, as other targets use internal memory for DMA operations. */
#if SOC_CACHE_INTERNAL_MEM_VIA_L1CACHE
    if (efuse_hal_flash_encryption_enabled()) {
        if (esp_ptr_external_ram(input) || esp_ptr_external_ram(output) || esp_ptr_in_drom(input) || esp_ptr_in_drom(output)) {
            input_alignment = MAX(get_cache_line_size(input), SOC_GDMA_EXT_MEM_ENC_ALIGNMENT);
            output_alignment = MAX(get_cache_line_size(output), SOC_GDMA_EXT_MEM_ENC_ALIGNMENT);

            input_heap_caps = MALLOC_CAP_8BIT | (esp_ptr_external_ram(input) ? MALLOC_CAP_SPIRAM : MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
            output_heap_caps = MALLOC_CAP_8BIT | (esp_ptr_external_ram(output) ? MALLOC_CAP_SPIRAM : MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        }
    }
#endif /* SOC_CACHE_INTERNAL_MEM_VIA_L1CACHE */

    if (realloc_input) {
        input_buf = heap_caps_aligned_alloc(input_alignment, chunk_len, input_heap_caps);
        if (input_buf == NULL) {
            mbedtls_platform_zeroize(output, len);
            ESP_LOGE(TAG, "Failed to allocate memory");
            return -1;
        }
    }

    if (realloc_output) {
        output_buf = heap_caps_aligned_alloc(output_alignment, chunk_len, output_heap_caps);
        if (output_buf == NULL) {
            mbedtls_platform_zeroize(output, len);
            ESP_LOGE(TAG, "Failed to allocate memory");
            return -1;
        }
    } else {
        output_buf = output;
    }

    while (len) {
        chunk_len = MIN(AES_MAX_CHUNK_WRITE_SIZE, len);

        /* If input needs realloc then copy it, else use the input with offset*/
        if (realloc_input) {
            memcpy(input_buf, input + offset, chunk_len);
            dma_input = input_buf;
        } else {
            dma_input = input + offset;
        }

        if (esp_aes_process_dma(ctx, dma_input, output_buf, chunk_len, stream_out) != 0) {
            ret = -1;
            goto cleanup;
        }

        if (realloc_output) {
            memcpy(output + offset, output_buf, chunk_len);
        } else {
            output_buf = output + offset + chunk_len;
        }

        len -= chunk_len;
        offset += chunk_len;
    }

cleanup:

    if (realloc_input) {
        free(input_buf);
    }
    if (realloc_output) {
        free(output_buf);
    }

    return ret;
}
