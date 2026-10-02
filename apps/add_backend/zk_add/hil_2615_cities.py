"""Reviewed city identities and their existing factory images for 2.6.15 HIL."""

from zk_add.hil_scope import HilTarget


CITY_TARGETS = {
    "ZONE-FAISALABAD-01": HilTarget(connector_id="beb5f8eb-e4f3-4620-8f1a-cfa460bb1b73",
        mac="ac:27:6e:a5:5a:20", terminal_serial="CKPG221260245"),
    "ZONE-FAISALABAD-02": HilTarget(connector_id="ab5f934a-5430-4ba2-a86d-5ac849276e0d",
        mac="a4:cb:8f:d4:61:ac", terminal_serial="CKPG221260316"),
    "ZONE-MULTAN-01": HilTarget(connector_id="0e991162-5ab9-467a-a952-4dc4c420691c",
        mac="ac:27:6e:a3:10:0c", terminal_serial="RKQ4245100152"),
    "ZONE-MULTAN-02": HilTarget(connector_id="06d8706c-8b9e-4a48-b8c7-a76d16da6ed4",
        mac="ac:27:6e:a3:de:e8", terminal_serial="AF4C211861133"),
    "ZONE-LAHORE-01": HilTarget(connector_id="9726fe6c-2905-447a-9041-2fa606660058",
        mac="ac:27:6e:a3:16:d4", terminal_serial="AEH2232460004"),
    "ZONE-LAHORE-02": HilTarget(connector_id="8727e27a-77be-41c7-bb9e-5a7f31e4ae67",
        mac="ac:27:6e:a4:54:b4", terminal_serial="CKPG221260408"),
    "ZONE-QUETTA-01": HilTarget(connector_id="474fd36e-6c75-4e0d-9c1f-cc97c4a442f8",
        mac="ac:27:6e:a3:07:f8", terminal_serial="OCN6060066052700045"),
    "ZONE-KARACHI-01": HilTarget(connector_id="7d6fadcc-f93e-4c35-b4cc-852af1ffac0c",
        mac="a4:cb:8f:d4:67:70", terminal_serial="RKQ4254900154"),
}

# (currently installed factory version, exact application digest, bridge version).
# Once the bridge succeeds, the ordinary 2.6.15 predecessor checks apply.
CITY_FACTORY_PREDECESSORS = {
    "ZONE-FAISALABAD-01": ("2.4.12", "d5dd25324c47056370c755604de8edcf34038c2ef4ae2aeb4013557ace5e9e26", "2.5.2"),
    "ZONE-FAISALABAD-02": ("2.4.12", "00de32ec6d41344feaae39bf4a35c9592f708d684eca286efb04f8d343195101", "2.5.2"),
    "ZONE-MULTAN-01": ("2.4.12", "c3d2605cbd44f801516c87ef6406b5b0bb51d560e1dda9ef746c4a71b15da7b4", "2.5.2"),
    "ZONE-MULTAN-02": ("2.4.12", "4c7f3e29eed08f9f0ba68ef45d51f6ad53d6e42dcc81e6780b39b09d05909ea9", "2.5.2"),
    "ZONE-LAHORE-01": ("2.5.2", "e972ba4c56fa5763abf56c82d24f70c1e990285e2728a6a842a9f1121bb0df37", "2.4.12"),
    "ZONE-LAHORE-02": ("2.5.2", "2d092070c67f587b167348319495672f9a5e19b3ebbaf36dba13618bf17be424", "2.4.12"),
    "ZONE-QUETTA-01": ("2.4.12", "d889e83460b0baa3d6c81784a603d17b80640b57d65a40161bf5097a32d4670d", "2.5.2"),
}

# (release ID, version, source SHA, signed artifact SHA, application SHA).
SIGNED_BRIDGE_IDENTITIES = {
    "2.4.12": (
        "zone-lite-2.4.12", "2.4.12", "45690c400057eb343828fa5c13f4865866f8ed9c",
        "7a939dff0f9e9787faf47a22ef2b6de21aa0a0352f04e3eae4762c2530107010",
        "cf9e6e2deff0a237b0bb007fe95e2468fab2503fbceccc8d91c7834f0a6ba589",
    ),
    "2.5.2": (
        "zone-lite-2.5.2", "2.5.2", "c8c5a0e91aa6ff3d12adce0f1f48b8110ea4b532",
        "e818e8e7db5d9aa1c92b798d03d088026b36bbe9f4672f908450aa4aa85ef564",
        "4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b",
    ),
}
