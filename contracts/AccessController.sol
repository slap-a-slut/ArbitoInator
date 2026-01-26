// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/AccessControl.sol";

/**
 * @title AccessController
 * @dev Centralized access control for ArbitoInator system
 * @notice Manages roles for executor, admin, and other privileged operations
 */
contract AccessController is AccessControl {
    // Role definitions
    bytes32 public constant EXECUTOR_ROLE = keccak256("EXECUTOR_ROLE");
    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");
    bytes32 public constant SWEEPER_ROLE = keccak256("SWEEPER_ROLE");
    bytes32 public constant MANAGER_ROLE = keccak256("MANAGER_ROLE");
    
    /**
     * @dev Constructor sets up initial roles
     * @param admin Address to grant DEFAULT_ADMIN_ROLE
     */
    constructor(address admin) {
        if (admin == address(0)) revert("Invalid admin address");
        
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(ADMIN_ROLE, admin);
        _grantRole(EXECUTOR_ROLE, admin);
        _grantRole(SWEEPER_ROLE, admin);
        _grantRole(MANAGER_ROLE, admin);
    }
    
    /**
     * @dev Check if address has executor role
     */
    function isExecutor(address account) public view returns (bool) {
        return hasRole(EXECUTOR_ROLE, account);
    }
    
    /**
     * @dev Check if address has admin role
     */
    function isAdmin(address account) public view returns (bool) {
        return hasRole(ADMIN_ROLE, account);
    }
    
    /**
     * @dev Check if address has sweeper role
     */
    function isSweeper(address account) public view returns (bool) {
        return hasRole(SWEEPER_ROLE, account);
    }
    
    /**
     * @dev Check if address has manager role
     */
    function isManager(address account) public view returns (bool) {
        return hasRole(MANAGER_ROLE, account);
    }
    
    /**
     * @dev Grant executor role (admin only)
     */
    function grantExecutorRole(address account) public onlyRole(DEFAULT_ADMIN_ROLE) {
        _grantRole(EXECUTOR_ROLE, account);
    }
    
    /**
     * @dev Revoke executor role (admin only)
     */
    function revokeExecutorRole(address account) public onlyRole(DEFAULT_ADMIN_ROLE) {
        _revokeRole(EXECUTOR_ROLE, account);
    }
    
    /**
     * @dev Grant admin role (only DEFAULT_ADMIN_ROLE)
     */
    function grantAdminRole(address account) public onlyRole(DEFAULT_ADMIN_ROLE) {
        _grantRole(ADMIN_ROLE, account);
    }
    
    /**
     * @dev Revoke admin role (only DEFAULT_ADMIN_ROLE)
     */
    function revokeAdminRole(address account) public onlyRole(DEFAULT_ADMIN_ROLE) {
        _revokeRole(ADMIN_ROLE, account);
    }
}