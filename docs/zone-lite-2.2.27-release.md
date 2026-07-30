# Zone Lite 2.2.27 corrective release

Zone Lite 2.2.27 retains the bounded asynchronous inbound catalog processing
from 2.2.26 while using the proven 8 KiB ADD task stack profile. The SWAT
2.2.26 candidate stayed offline during protected first boot, consistent with
the 12 KiB worker allocation being unavailable on the deployed ESP memory
profile.

Worker startup is now an explicit OTA boot-health precondition. If allocation
fails, the connector remains fail-closed and the candidate cannot be marked
valid. The bootloader therefore returns to the last accepted image without
exposing the candidate nationwide.
