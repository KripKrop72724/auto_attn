#include "cJSON.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static cJSON_Hooks hooks = {malloc, free};
void cJSON_InitHooks(cJSON_Hooks *installed) { hooks = *installed; }

static char *copy(const char *text)
{
    char *out = hooks.malloc_fn(strlen(text) + 1);
    if (out) strcpy(out, text);
    return out;
}

static cJSON *node(const char *key)
{
    cJSON *item = hooks.malloc_fn(sizeof(*item));
    if (!item) return NULL;
    memset(item, 0, sizeof(*item));
    if (key && !(item->key = copy(key))) {
        hooks.free_fn(item);
        return NULL;
    }
    return item;
}

static void attach(cJSON *parent, cJSON *child)
{
    cJSON **slot = &parent->child;
    while (*slot) slot = &(*slot)->next;
    *slot = child;
}

static cJSON *add(cJSON *object, const char *key)
{
    cJSON *item = object ? node(key) : NULL;
    if (item) attach(object, item);
    return item;
}

cJSON *cJSON_CreateObject(void)
{
    cJSON *item = node(NULL);
    if (item) item->is_object = 1;
    return item;
}
cJSON *cJSON_AddObjectToObject(cJSON *object, const char *key)
{
    cJSON *item = add(object, key);
    if (item) item->is_object = 1;
    return item;
}
cJSON *cJSON_AddArrayToObject(cJSON *object, const char *key)
{
    cJSON *item = add(object, key);
    if (item) item->is_array = 1;
    return item;
}
cJSON *cJSON_AddNumberToObject(cJSON *object, const char *key, double number)
{
    cJSON *item = add(object, key);
    if (item) item->number = number;
    return item;
}
cJSON *cJSON_AddStringToObject(cJSON *object, const char *key, const char *text)
{
    char *value = copy(text);
    if (!value) return NULL;
    cJSON *item = add(object, key);
    if (!item) {
        hooks.free_fn(value);
        return NULL;
    }
    item->is_string = 1;
    item->text = value;
    return item;
}
cJSON_bool cJSON_AddItemToArray(cJSON *array, cJSON *item)
{
    if (!array || !item) return 0;
    attach(array, item);
    return 1;
}
void cJSON_Delete(cJSON *item)
{
    while (item) {
        cJSON *next = item->next;
        cJSON_Delete(item->child);
        hooks.free_fn(item->key);
        hooks.free_fn(item->text);
        hooks.free_fn(item);
        item = next;
    }
}

static void print(const cJSON *item, char *out, size_t size, int in_array)
{
    size_t length = strlen(out);
    if (!in_array && item->key) length += (size_t)snprintf(out + length, size - length, "\"%s\":", item->key);
    if (item->is_string) {
        snprintf(out + length, size - length, "\"%s\"", item->text);
    } else if (item->is_object || item->is_array) {
        int array = item->is_array;
        snprintf(out + length, size - length, "%c", array ? '[' : '{');
        for (const cJSON *child = item->child; child; child = child->next) {
            print(child, out, size, array);
            if (child->next) strncat(out, ",", size - strlen(out) - 1);
        }
        strncat(out, array ? "]" : "}", size - strlen(out) - 1);
    } else {
        snprintf(out + length, size - length, "%.0f", item->number);
    }
}

char *cJSON_PrintUnformatted(const cJSON *item)
{
    char *out = calloc(1, 8192);
    if (out) print(item, out, 8192, 1); /* a printed root never carries its own key */
    return out;
}
