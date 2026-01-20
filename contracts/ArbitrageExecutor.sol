// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./Interfaces.sol";

contract ArbitrageExecutor {
    address public owner;

    constructor() {
        owner = msg.sender;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    struct SwapRoute {
        address tokenIn;
        address tokenOut;
        uint256 amountIn;
        uint256 amountOutMin;
        bytes dexData; // encoded routing
    }

    receive() external payable {}

    function execute(
        SwapRoute[] calldata routes,
        uint256 minProfit,
        address profitToken,
        address to
    ) external onlyOwner returns (uint256 profit) {

        uint256 startBal = IERC20(profitToken).balanceOf(address(this));

        for (uint256 i; i < routes.length; i++) {
            _swap(routes[i]);
        }

        uint256 endBal = IERC20(profitToken).balanceOf(address(this));
        profit = endBal - startBal;

        require(profit >= minProfit, "not profitable");

        IERC20(profitToken).transfer(to, profit);
    }

    // -------- INTERNAL SWAP ROUTING --------

    function _swap(SwapRoute calldata route) internal {
        (uint8 dex, bytes memory data) = abi.decode(route.dexData, (uint8, bytes));

        if (dex == 1) {
            _swapUniV2(route, data);
        } else if (dex == 2) {
            _swapUniV3(route, data);
        } else {
            revert("unknown dex");
        }
    }

    function _swapUniV2(SwapRoute calldata route, bytes memory data) internal {
        (address router, address[] memory path) = abi.decode(data, (address, address[]));

        // approve
        IERC20(route.tokenIn).approve(router, route.amountIn);

        // execute
        IUniswapV2Router(router).swapExactTokensForTokens(
            route.amountIn,
            route.amountOutMin > 0 ? route.amountOutMin : 1,
            path,
            address(this),
            block.timestamp
        );
    }

    function _swapUniV3(SwapRoute calldata route, bytes memory data) internal {
        (address router, uint24 fee) = abi.decode(data, (address, uint24));

        IERC20(route.tokenIn).approve(router, route.amountIn);

        IUniswapV3Router(router).exactInputSingle(
            IUniswapV3Router.ExactInputSingleParams({
                tokenIn: route.tokenIn,
                tokenOut: route.tokenOut,
                fee: fee,
                recipient: address(this),
                deadline: block.timestamp,
                amountIn: route.amountIn,
                amountOutMinimum: route.amountOutMin > 0 ? route.amountOutMin : 1,
                sqrtPriceLimitX96: 0   // no price limit
            })
        );
    }

    // -------- OWNER OPS --------

    function withdrawToken(address token, uint256 amount) external onlyOwner {
        IERC20(token).transfer(msg.sender, amount);
    }

    function withdrawETH(uint256 amount) external onlyOwner {
        (bool sent,) = msg.sender.call{value: amount}("");
        require(sent, "eth withdraw failed");
    }
}
