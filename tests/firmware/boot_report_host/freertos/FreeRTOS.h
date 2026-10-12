#pragma once
/* Critical sections are counted so the harness can prove the panic path
 * never takes one. */
typedef int portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED 0
extern int host_critical_depth, host_critical_entries;
#define portENTER_CRITICAL(mux) ((void)(mux), ++host_critical_depth, ++host_critical_entries)
#define portEXIT_CRITICAL(mux) ((void)(mux), --host_critical_depth)
#define portENTER_CRITICAL_SAFE(mux) portENTER_CRITICAL(mux)
#define portEXIT_CRITICAL_SAFE(mux) portEXIT_CRITICAL(mux)
