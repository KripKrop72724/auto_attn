#pragma once
/* A minimal cJSON subset with the same signatures, enough for host harnesses
 * to observe the exact tree firmware builds and the allocator it uses. Every
 * allocation goes through the installed hooks, and a failed one returns NULL
 * as real cJSON does. */
#include <stddef.h>
typedef int cJSON_bool;
typedef struct cJSON {
    struct cJSON *next, *child;
    int is_object, is_array, is_string;
    char *key, *text;
    double number;
} cJSON;
typedef struct {
    void *(*malloc_fn)(size_t size);
    void (*free_fn)(void *block);
} cJSON_Hooks;
void cJSON_InitHooks(cJSON_Hooks *hooks);
cJSON *cJSON_CreateObject(void);
cJSON *cJSON_AddObjectToObject(cJSON *object, const char *key);
cJSON *cJSON_AddArrayToObject(cJSON *object, const char *key);
cJSON *cJSON_AddNumberToObject(cJSON *object, const char *key, double number);
cJSON *cJSON_AddStringToObject(cJSON *object, const char *key, const char *text);
cJSON_bool cJSON_AddItemToArray(cJSON *array, cJSON *item);
void cJSON_Delete(cJSON *item);
/* Unlike cJSON, strings are printed unescaped and the result is plain
 * malloc'd memory. */
char *cJSON_PrintUnformatted(const cJSON *item);
