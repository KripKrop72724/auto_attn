import { describe, expect, it } from 'vitest'
import { provisioningActiveStep, provisioningConfigurationSchema, hikvisionConfigurationSchema } from './FirmwareProvisioning'

const valid = {
  wifi_ssid: 'State Life Office', wifi_password: 'correct horse battery staple',
  communication_key: '4294967295', zkt_port: '4370', preferred_ip: '0.0.0.0',
  device_id: 'ZKT.01', zone_id: 'ZONE-PESH-01', zone_name: 'Peshawar Branch 1',
}

describe('physical provisioning contract', () => {
  it('maps durable backend states onto the ordered operator flow', () => {
    expect(provisioningActiveStep()).toBe(0)
    expect(provisioningActiveStep('WAITING_FOR_DEVICE')).toBe(1)
    expect(provisioningActiveStep('CONFIGURING')).toBe(2)
    expect(provisioningActiveStep('AWAITING_AUTHORIZATION')).toBe(3)
    expect(provisioningActiveStep('READBACK_VERIFYING')).toBe(4)
    expect(provisioningActiveStep('SITE_VALIDATION_PENDING')).toBe(5)
  })

  it('accepts exact boundaries and the discovery sentinel', () => {
    expect(provisioningConfigurationSchema.safeParse(valid).success).toBe(true)
    expect(provisioningConfigurationSchema.safeParse({ ...valid, wifi_password: 'a'.repeat(64) }).success).toBe(true)
    expect(provisioningConfigurationSchema.safeParse({ ...valid, preferred_ip: '192.168.20.4' }).success).toBe(true)
  })

  it.each([
    ['wifi_ssid', 'é'.repeat(17)], ['wifi_password', 'short'],
    ['communication_key', '4294967296'], ['communication_key', 'not-a-number'], ['zkt_port', '0'],
    ['preferred_ip', '8.8.8.8'], ['device_id', 'x'.repeat(32)],
    ['zone_id', 'has spaces'], ['zone_name', ' trailing '],
  ])('rejects unsafe %s values', (field, value) => {
    expect(provisioningConfigurationSchema.safeParse({ ...valid, [field]: value }).success).toBe(false)
  })
})


describe('Hikvision provisioning', () => {
  const hikvision = { ...valid, communication_key: '', hik_host: '192.168.10.20',
    hik_port: '80', hik_transport: 'http_digest', hik_username: 'admin',
    hik_password: 'test-only-secret', hik_expected_serial: 'terminal-1',
    hik_profile: 'qualified-profile', hik_source_epoch: 'verified-epoch', hik_ca_pem: '',
  }
  it('accepts device credentials without a ZKT communication key', () => {
    expect(hikvisionConfigurationSchema.safeParse(hikvision).success).toBe(true)
  })
  it.each(['hik_expected_serial', 'hik_source_epoch', 'hik_password', 'hik_profile'])(
    'requires %s before preflight', key => {
      expect(hikvisionConfigurationSchema.safeParse({ ...hikvision, [key]: '' }).success).toBe(false)
    },
  )
  it('rejects automatic discovery and public addresses', () => {
    for (const hik_host of ['0.0.0.0', '8.8.8.8']) {
      expect(hikvisionConfigurationSchema.safeParse({ ...hikvision, hik_host }).success).toBe(false)
    }
  })
})
