import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildWorkspaceTree,
  countWorkspaces,
  parseGroupParam,
  serializeGroupParam,
} from '../workspace-grouping'

function ws(id: string, name: string, vcsRepoUrl?: string, workingDir?: string) {
  return {
    id,
    attributes: {
      name,
      'vcs-repo-url': vcsRepoUrl,
      'working-directory': workingDir,
    },
  }
}

describe('parseGroupParam', () => {
  it('returns flat for null', () => {
    assert.equal(parseGroupParam(null), 'flat')
  })
  it('returns flat for unknown values', () => {
    assert.equal(parseGroupParam('bogus'), 'flat')
  })
  it('parses repo', () => {
    assert.equal(parseGroupParam('repo'), 'repo')
  })
  it('parses repo-path', () => {
    assert.equal(parseGroupParam('repo-path'), 'repo-path')
  })
})

describe('serializeGroupParam', () => {
  it('returns null for flat', () => {
    assert.equal(serializeGroupParam('flat'), null)
  })
  it('returns mode string for non-flat', () => {
    assert.equal(serializeGroupParam('repo'), 'repo')
    assert.equal(serializeGroupParam('repo-path'), 'repo-path')
  })
})

describe('buildWorkspaceTree', () => {
  it('returns empty array for flat mode', () => {
    const result = buildWorkspaceTree([ws('1', 'test', 'https://github.com/org/repo')], 'flat')
    assert.deepEqual(result, [])
  })

  it('groups by repo only in repo mode', () => {
    const workspaces = [
      ws('1', 'infra-dev', 'https://github.com/org/infra.git', 'environments/dev'),
      ws('2', 'infra-prod', 'https://github.com/org/infra.git', 'environments/prod'),
      ws('3', 'app-deploy', 'https://github.com/org/app.git'),
    ]
    const result = buildWorkspaceTree(workspaces, 'repo')
    assert.equal(result.length, 2)
    assert.equal(result[0].label, 'app')
    assert.equal(result[0].workspaces.length, 1)
    assert.equal(result[0].children.length, 0)
    assert.equal(result[1].label, 'infra')
    assert.equal(result[1].workspaces.length, 2)
    assert.equal(result[1].children.length, 0)
  })

  it('groups by repo then path in repo-path mode', () => {
    const workspaces = [
      ws('1', 'infra-dev', 'https://github.com/org/infra.git', 'environments/dev'),
      ws('2', 'infra-prod', 'https://github.com/org/infra.git', 'environments/prod'),
      ws('3', 'infra-root', 'https://github.com/org/infra.git'),
    ]
    const result = buildWorkspaceTree(workspaces, 'repo-path')
    assert.equal(result.length, 1)
    const infra = result[0]
    assert.equal(infra.label, 'infra')
    assert.equal(infra.workspaces.length, 1)
    assert.equal(infra.workspaces[0].name, 'infra-root')
    assert.equal(infra.children.length, 1)
    assert.equal(infra.children[0].label, 'environments')
    assert.equal(infra.children[0].children.length, 2)
  })

  it('handles leading slashes in working-directory', () => {
    const workspaces = [
      ws('1', 'test', 'https://github.com/org/repo.git', '/leading/slash'),
    ]
    const result = buildWorkspaceTree(workspaces, 'repo-path')
    const repo = result[0]
    assert.equal(repo.children[0].label, 'leading')
    assert.equal(repo.children[0].children[0].label, 'slash')
  })

  it('handles empty working-directory', () => {
    const workspaces = [
      ws('1', 'root-ws', 'https://github.com/org/repo.git', ''),
    ]
    const result = buildWorkspaceTree(workspaces, 'repo-path')
    assert.equal(result[0].workspaces.length, 1)
    assert.equal(result[0].workspaces[0].name, 'root-ws')
  })

  it('separates local workspaces from VCS workspaces', () => {
    const workspaces = [
      ws('1', 'vcs-ws', 'https://github.com/org/repo.git'),
      ws('2', 'local-ws'),
    ]
    const result = buildWorkspaceTree(workspaces, 'repo')
    assert.equal(result.length, 2)
    assert.equal(result[0].label, 'repo')
    assert.equal(result[1].label, 'Local')
    assert.equal(result[1].workspaces[0].name, 'local-ws')
  })

  it('handles SSH-style repo URLs', () => {
    const workspaces = [
      ws('1', 'ssh-ws', 'git@github.com:org/my-repo.git'),
    ]
    const result = buildWorkspaceTree(workspaces, 'repo')
    assert.equal(result[0].label, 'my-repo')
  })

  it('does not merge repos with the same basename from different orgs', () => {
    const workspaces = [
      ws('1', 'org-a-infra', 'https://github.com/org-a/infra.git'),
      ws('2', 'org-b-infra', 'https://github.com/org-b/infra.git'),
    ]
    const result = buildWorkspaceTree(workspaces, 'repo')
    assert.equal(result.length, 2)
    const names = result.map(g => g.workspaces[0].name).sort()
    assert.deepEqual(names, ['org-a-infra', 'org-b-infra'])
  })

  it('sorts groups and workspaces alphabetically', () => {
    const workspaces = [
      ws('1', 'zulu', 'https://github.com/org/bravo.git'),
      ws('2', 'alpha', 'https://github.com/org/bravo.git'),
      ws('3', 'ws', 'https://github.com/org/alpha.git'),
    ]
    const result = buildWorkspaceTree(workspaces, 'repo')
    assert.equal(result[0].label, 'alpha')
    assert.equal(result[1].label, 'bravo')
    assert.equal(result[1].workspaces[0].name, 'alpha')
    assert.equal(result[1].workspaces[1].name, 'zulu')
  })
})

describe('countWorkspaces', () => {
  it('counts workspaces recursively', () => {
    const workspaces = [
      ws('1', 'a', 'https://github.com/org/repo.git', 'env/dev'),
      ws('2', 'b', 'https://github.com/org/repo.git', 'env/prod'),
      ws('3', 'c', 'https://github.com/org/repo.git'),
    ]
    const result = buildWorkspaceTree(workspaces, 'repo-path')
    assert.equal(countWorkspaces(result[0]), 3)
  })
})
