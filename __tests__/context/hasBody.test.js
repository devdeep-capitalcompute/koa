'use strict'

const { describe, it } = require('node:test')
const assert = require('node:assert/strict')
const context = require('../../test-helpers/context')

describe('ctx.hasBody', () => {
  it('reports whether a response body has been set', () => {
    const ctx = context()

    assert.strictEqual(ctx.hasBody, false, 'VERITY_ASSERT:has_body_unassigned')

    for (const body of ['', 'hello', { hello: 'world' }, Buffer.from('hello')]) {
      ctx.body = body
      assert.strictEqual(ctx.hasBody, true, 'VERITY_ASSERT:has_body_assigned')
    }

    ctx.body = null
    assert.strictEqual(ctx.hasBody, false, 'VERITY_ASSERT:has_body_null')
    console.log('VERITY_PASS:has_body_states')
  })
})
