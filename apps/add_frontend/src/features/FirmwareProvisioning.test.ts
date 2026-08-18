import { describe, expect, it } from 'vitest'
import { provisioningActiveStep, provisioningConfigurationSchema } from './FirmwareProvisioning'

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
