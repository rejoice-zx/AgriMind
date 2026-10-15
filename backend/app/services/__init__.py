"""Business services are imported explicitly from their owning modules.

Avoid eagerly activating retired product, human-service and statistics modules
when an agricultural service is imported. Repository consumers use submodules.
"""
