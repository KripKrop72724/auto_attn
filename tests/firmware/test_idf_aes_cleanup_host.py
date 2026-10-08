"""Reproduce and fix the pinned SDK's second-bounce-allocation leak in real C."""

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "firmware/zone_lite/cmake/patch_idf_aes.py"
spec = importlib.util.spec_from_file_location("patch_idf_aes", PATCH)
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)
FUNCTION_SHA256 = "3a5064cd6ea278f40d87b5e3d5fc8973723b92dfcbfa3208a712dc27f9880c09"


def _function(source):
    start = source.index("static int esp_aes_process_dma_ext_ram(")
    opening = source.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end] + "\n"


def test_unknown_sdk_and_ambiguous_patch_fail_closed(tmp_path):
    original = tmp_path / "sdk.c"
    destination = tmp_path / "generated.c"
    original.write_text(patch.ORIGINAL)
    destination.write_text("previous build remains intact")
    with pytest.raises(ValueError, match="build-local copy"):
        patch.generate(original, original)
    with pytest.raises(ValueError, match="Unknown ESP-IDF"):
        patch.generate(original, destination)
    assert destination.read_text() == "previous build remains intact"
    for source in ("", patch.ORIGINAL + patch.ORIGINAL, patch.CORRECTED):
        with pytest.raises(ValueError, match="source context"):
            patch.patch_failure_path(source)


def test_sdk_fixture_matches_reviewed_source_and_build_copy(tmp_path):
    fixture = _function((ROOT / "tests/firmware/fixtures/esp_idf_553_aes_bounce.inc").read_text())
    assert hashlib.sha256(fixture.encode()).hexdigest() == FUNCTION_SHA256
    idf = Path(os.environ.get("IDF_PATH", Path.home() / "esp/esp-idf-v5.5.3"))
    original = idf / "components/mbedtls/port/aes/dma/esp_aes_dma_core.c"
    # Plain host CI still runs the hash-pinned exact function regression below.
    # Actual IDF builds always enforce the complete source hash at configure.
    if original.is_file():
        data = original.read_bytes()
        assert hashlib.sha256(data).hexdigest() == patch.SOURCE_SHA256
        assert _function(data.decode()) == fixture
        destination = tmp_path / "generated.c"
        patch.generate(original, destination)
        assert _function(destination.read_text()) == patch.patch_failure_path(fixture)
        before = destination.stat().st_mtime_ns
        patch.generate(original, destination)
        assert destination.stat().st_mtime_ns == before
        assert original.read_bytes() == data


def test_actual_cmake_source_scope_normalizes_generated_file_macro(tmp_path):
    """Exercise the real target replacement with differing roots/output names."""
    cmake = shutil.which("cmake")
    if not cmake:
        pytest.skip("CMake is required for the generated-source build regression")
    results = []
    for name in ("first-root", "second-root"):
        project = tmp_path / name
        sdk = project / "sdk"
        original = sdk / "components/mbedtls/port/aes/dma/esp_aes_dma_core.c"
        original.parent.mkdir(parents=True)
        original.write_text("const char *source_name(void){return __FILE__;}\n")
        (project / "crypto").mkdir()
        (project / "cmake").mkdir()
        (project / "crypto/CMakeLists.txt").write_text(
            'add_library(mbedcrypto STATIC "$ENV{IDF_PATH}/components/'
            'mbedtls/port/aes/dma/esp_aes_dma_core.c")\n'
        )
        # The patch's cryptographic-source pin is covered separately above. This
        # synthetic input isolates real CMake target/source property scoping.
        shutil.copyfile(
            ROOT / "firmware/zone_lite/cmake/idf_aes_cleanup.cmake",
            project / "cmake/idf_aes_cleanup.cmake",
        )
        (project / "cmake/patch_idf_aes.py").write_text(
            "from pathlib import Path\nimport sys\n"
            "out=Path(sys.argv[2]);out.parent.mkdir(parents=True,exist_ok=True)\n"
            "out.write_bytes(Path(sys.argv[1]).read_bytes())\n"
        )
        (project / "main.c").write_text(
            "#include <stdio.h>\nconst char *source_name(void);\n"
            "int main(void){puts(source_name());return 0;}\n"
        )
        (project / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.18)\nproject(probe C)\n"
            'add_compile_options("-fmacro-prefix-map=${CMAKE_SOURCE_DIR}=.")\n'
            "add_subdirectory(crypto)\ninclude(cmake/idf_aes_cleanup.cmake)\n"
            "add_executable(probe main.c)\ntarget_link_libraries(probe mbedcrypto)\n"
        )
        build = project / ("build-" + name)
        env = dict(os.environ, IDF_PATH=str(sdk))
        subprocess.run(
            [cmake, "-S", str(project), "-B", str(build), f"-DPYTHON={sys.executable}"],
            env=env, check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            [cmake, "--build", str(build)], env=env, check=True,
            capture_output=True, timeout=30,
        )
        results.append(subprocess.check_output([str(build / "probe")], timeout=10))
    assert results == [b"/IDF_BUILD/generated/esp_aes_dma_core_cleanup.c\n"] * 2


@pytest.mark.parametrize("corrected", [False, True])
def test_actual_aes_function_failures_success_and_repeated_retry(tmp_path, corrected):
    actual = _function((ROOT / "tests/firmware/fixtures/esp_idf_553_aes_bounce.inc").read_text())
    if corrected:
        actual = patch.patch_failure_path(actual)
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define MIN(a,b) ((a)<(b)?(a):(b))
#define AES_MAX_CHUNK_WRITE_SIZE 1600
#define MALLOC_CAP_DMA 8U
#define SOC_CACHE_INTERNAL_MEM_VIA_L1CACHE 0
#define ESP_LOGE(...) ((void)0)
typedef struct {int unused;} esp_aes_context;
static void *allocated[2];
static size_t sizes[2],live;
static unsigned calls,fail_on,crypto_calls;
static bool crypto_fail;
static void *heap_caps_aligned_alloc(size_t alignment,size_t bytes,uint32_t caps){
 assert(alignment==1 && caps==8 && bytes && bytes<=1600);++calls;
 if(calls==fail_on)return NULL;
 for(unsigned i=0;i<2;i++)if(!allocated[i]){
  allocated[i]=malloc(bytes);assert(allocated[i]);sizes[i]=bytes;live+=bytes;return allocated[i];
 }
 assert(false);return NULL;
}
static void tracked_free(void *ptr){
 if(!ptr)return;
 for(unsigned i=0;i<2;i++)if(allocated[i]==ptr){live-=sizes[i];free(ptr);allocated[i]=NULL;sizes[i]=0;return;}
 assert(false);
}
static void mbedtls_platform_zeroize(void *ptr,size_t bytes){memset(ptr,0,bytes);}
static int esp_aes_process_dma(esp_aes_context *ctx,const unsigned char *input,unsigned char *output,size_t len,uint8_t *stream){
 (void)ctx;(void)stream;++crypto_calls;if(crypto_fail)return -1;memcpy(output,input,len);return 0;
}
#define free tracked_free
/* ACTUAL */
#undef free
static void reset(unsigned fail,bool crypto){assert(!live);calls=crypto_calls=0;fail_on=fail;crypto_fail=crypto;}
int main(void){
 esp_aes_context ctx={0};unsigned char input[4096],output[4096];memset(input,0x5a,sizeof(input));
 reset(1,false);memset(output,0xa5,sizeof(output));
 assert(esp_aes_process_dma_ext_ram(&ctx,input,output,1024,NULL,true,true)==-1);
 assert(calls==1 && !live && !crypto_calls);for(unsigned i=0;i<1024;i++)assert(!output[i]);
 reset(2,false);memset(output,0xa5,sizeof(output));
 assert(esp_aes_process_dma_ext_ram(&ctx,input,output,1024,NULL,true,true)==-1);
 assert(calls==2 && !crypto_calls);for(unsigned i=0;i<1024;i++)assert(!output[i]);
 #if CORRECTED
 assert(!live);
 #else
 /* Reproduce the exact upstream defect, then clean test bookkeeping. */
 assert(live==1024);tracked_free(allocated[0]);
 #endif
 for(unsigned flags=1;flags<4;flags++){
  bool in=flags&1,out=flags&2;
  reset(1,false);assert(esp_aes_process_dma_ext_ram(&ctx,input,output,1024,NULL,in,out)==-1 && !live);
  reset(0,true);assert(esp_aes_process_dma_ext_ram(&ctx,input,output,4096,NULL,in,out)==-1 && !live && crypto_calls==1);
  reset(0,false);memset(output,0,sizeof(output));
  assert(!esp_aes_process_dma_ext_ram(&ctx,input,output,sizeof(input),NULL,in,out));
  assert(!live && crypto_calls==3 && !memcmp(input,output,sizeof(input)));
 }
 #if CORRECTED
 for(unsigned i=0;i<100;i++){
  reset(2,false);assert(esp_aes_process_dma_ext_ram(&ctx,input,output,1024,NULL,true,true)==-1 && !live);
  reset(0,false);assert(!esp_aes_process_dma_ext_ram(&ctx,input,output,1024,NULL,true,true) && !live);
 }
 #endif
 return 0;
}
'''.replace("/* ACTUAL */", actual)
    unit = tmp_path / "aes.c"
    unit.write_text(harness)
    binary = tmp_path / "aes"
    subprocess.run(
        [shutil.which("cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-fsanitize=address,undefined", f"-DCORRECTED={int(corrected)}", str(unit), "-o", str(binary)],
        check=True,
    )
    subprocess.run([str(binary)], check=True, timeout=10)
