// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/AccessControl.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

contract Treasury is AccessControl {
    using SafeERC20 for IERC20;

    bytes32 public constant ADMIN_ROLE = DEFAULT_ADMIN_ROLE;
    bytes32 public constant SWEEPER_ROLE = keccak256("SWEEPER_ROLE");

    constructor(address admin, address sweeper) {
        _grantRole(ADMIN_ROLE, admin);
        if (sweeper != address(0)) _grantRole(SWEEPER_ROLE, sweeper);
    }

    function sweepToken(address token, address to, uint256 amount) external onlyRole(SWEEPER_ROLE) {
        IERC20(token).safeTransfer(to, amount);
    }

    function sweepETH(address payable to, uint256 amount) external onlyRole(SWEEPER_ROLE) {
        (bool ok,) = to.call{value: amount}("");
        require(ok, "TREASURY_ETH_SEND_FAIL");
    }

    receive() external payable {}
}
