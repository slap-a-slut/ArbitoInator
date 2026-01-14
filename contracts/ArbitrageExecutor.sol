// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./Interfaces.sol";

contract ArbitrageExecutor {
    address public owner;

    constructor() {
        owner = payable(msg.sender);
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    // входные данные для арба
    struct SwapRoute {
        address tokenIn;
        address tokenOut;
        uint256 amountIn;
        bytes dexData; // info about path, pool, dex-type
    }

    receive() external payable {}

    function execute(
        SwapRoute[] calldata routes,
        uint256 minProfit,
        address profitToken,
        address to
    ) external onlyOwner returns (uint256 profit) {

        uint256 startBal = IERC20(profitToken).balanceOf(address(this));

        for (uint256 i = 0; i < routes.length; i++) {
            _swap(routes[i]);
        }

        uint256 endBal = IERC20(profitToken).balanceOf(address(this));
        profit = endBal - startBal;

        require(profit >= minProfit, "not profitable");

        IERC20(profitToken).transfer(to, profit);
    }

    function _swap(SwapRoute calldata route) internal {
        // placeholder — тут будет разветвление: curve, uniV2, uniV3, balancer и тд
        // swap через интерфейсы
    }

    function withdrawToken(address token, uint256 amount) external onlyOwner {
        IERC20(token).transfer(owner, amount);
    }

    function withdrawETH(uint256 amount) external onlyOwner {
        (bool sent, ) = owner.call{value: amount}("");
        require(sent);
    }
}
