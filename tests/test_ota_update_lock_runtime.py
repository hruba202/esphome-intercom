"""Exercise the upstream OTA lock and erase boundaries with real atomic operations."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_update_lock_rejects_second_owner_and_keeps_success_until_reboot(tmp_path):
    directory = ROOT / 'esphome/components/ota'
    header = (directory / 'ota_backend.h').read_text()
    begin = header.index('class UpdateLock {')
    end = header.index('\n};', begin) + 3
    declaration = header[begin:end]
    source = (directory / 'ota_backend.cpp').read_text()
    begin = source.index('// Non-blocking;')
    end = source.rindex('}  // namespace')
    body = source[begin:end]
    math_begin = header.index('static constexpr size_t OTA_BLOCK_ERASE_SIZE')
    math_end = header.index('\nenum OTAState', math_begin)
    cpp = tmp_path / 'lock.cpp'
    cpp.write_text(r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstddef>
#include <thread>
#include <type_traits>
#define USE_ESP32
namespace esphome::ota {
''' + declaration + '\n' + body + '\n' + header[math_begin:math_end] + r'''
struct TestLock : UpdateLock {using UpdateLock::force_release;};
}
int main(){
 using namespace esphome::ota;
 static_assert(!std::is_copy_constructible_v<UpdateLock>);
 {UpdateLock first;assert(first.try_acquire());assert(first.try_acquire());
  {UpdateLock rejected;assert(!rejected.try_acquire());rejected.release();}
  UpdateLock still_rejected;assert(!still_rejected.try_acquire());
 }
 {UpdateLock again;assert(again.try_acquire());again.hold_until_reboot();assert(!again.is_held());}
 UpdateLock after_success;assert(!after_success.try_acquire());TestLock::force_release();
 std::atomic<int> owners=0,errors=0,accepted=0;
 auto work=[&]{for(int i=0;i<10000;++i){UpdateLock lock;if(lock.try_acquire()){
  if(++owners!=1)++errors;++accepted;--owners;
 }}};
 std::thread a(work),b(work);a.join();b.join();assert(errors==0 && accepted>0 && owners==0);
 assert(next_erase_end(0,131072)==0);
 assert(next_erase_end(1,131072)==65536);
 assert(next_erase_end(65536,131072)==65536);
 assert(next_erase_end(65537,131072)==131072);
 assert(next_erase_end(70000,98304)==98304);
}
''')
    binary = tmp_path / 'lock'
    subprocess.run(['g++', '-std=c++20', '-pthread', str(cpp), '-o', str(binary)], check=True, capture_output=True, text=True)
    subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=10)
