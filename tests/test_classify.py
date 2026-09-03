from ado_build_minutes.classify import (
    DEPLOYMENT_GROUP,
    GITHUB_HOSTED,
    MANAGED_DEVOPS_POOL,
    MICROSOFT_HOSTED,
    SELF_HOSTED,
    VMSS_ELASTIC_POOL,
    classify_pool,
)


def test_classification_decision_tree_order():
    assert classify_pool({"poolType": "deployment", "isHosted": True, "name": "Azure Pipelines"}) == DEPLOYMENT_GROUP
    assert classify_pool({"poolType": "automation", "isHosted": True, "name": "GitHub-hosted Agents"}) == GITHUB_HOSTED
    assert classify_pool({"poolType": "automation", "isHosted": True, "name": "Azure Pipelines"}) == MICROSOFT_HOSTED
    assert classify_pool({"poolType": "automation", "isHosted": False, "options": ["elasticPool"]}) == VMSS_ELASTIC_POOL
    assert classify_pool({"poolType": "automation", "isHosted": False, "agentCloudId": "cloud-1"}) == MANAGED_DEVOPS_POOL
    assert classify_pool({"poolType": "automation", "isHosted": False, "name": "Default"}) == SELF_HOSTED


def test_elastic_pool_string_options_are_case_insensitive():
    assert classify_pool({"isHosted": False, "options": "elasticPool, singleUseAgents"}) == VMSS_ELASTIC_POOL
