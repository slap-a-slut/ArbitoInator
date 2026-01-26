// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./IDexAdapter.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/**
 * @title UniswapV2Adapter
 * @dev Adapter for Uniswap V2 style DEXes (including Sushiswap)
 * @notice Implements IDexAdapter for Uniswap V2 swap operations
 */
contract UniswapV2Adapter is IDexAdapter, IUniswapV2Adapter {
    using SafeERC20 for IERC20;
    
    address public immutable router;
    bool public paused;
    
    error Paused();
    error InvalidRouter();
    error InvalidPath();
    error InsufficientOutput();
    error ExpiredDeadline();
    error ZeroAddress();
    
    event SwapExecuted(
        address indexed tokenIn,
        address indexed tokenOut,
        uint256 amountIn,
        uint256 amountOut,
        address indexed recipient
    );
    
    constructor(address _router) {
        if (_router == address(0)) revert InvalidRouter();
        router = _router;
        paused = false;
    }
    
    function swap(bytes calldata data) external override whenNotPaused {
        (uint256 amountIn, uint256 amountOutMin, address[] memory path, address to, uint256 deadline) = 
            abi.decode(data, (uint256, uint256, address[], address, uint256));
        
        if (block.timestamp > deadline) revert ExpiredDeadline();
        if (path.length < 2) revert InvalidPath();
        if (path[0] == address(0) || path[path.length - 1] == address(0)) revert ZeroAddress();
        
        IERC20 tokenIn = IERC20(path[0]);
        tokenIn.safeTransferFrom(msg.sender, address(this), amountIn);
        tokenIn.forceApprove(router, amountIn);
        
        uint256[] memory amounts = IUniswapV2Router(router).swapExactTokensForTokens(
            amountIn,
            amountOutMin,
            path,
            to,
            deadline
        );
        
        uint256 amountOut = amounts[amounts.length - 1];
        if (amountOut < amountOutMin) revert InsufficientOutput();
        
        emit SwapExecuted(path[0], path[path.length - 1], amountIn, amountOut, to);
    }
    
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external override whenNotPaused returns (uint256[] memory amounts) {
        if (block.timestamp > deadline) revert ExpiredDeadline();
        if (path.length < 2) revert InvalidPath();
        
        IERC20 tokenIn = IERC20(path[0]);
        tokenIn.safeTransferFrom(msg.sender, address(this), amountIn);
        tokenIn.forceApprove(router, amountIn);
        
        amounts = IUniswapV2Router(router).swapExactTokensForTokens(
            amountIn,
            amountOutMin,
            path,
            to,
            deadline
        );
        
        uint256 amountOut = amounts[amounts.length - 1];
        emit SwapExecuted(path[0], path[path.length - 1], amountIn, amountOut, to);
    }
    
    function getName() external pure override returns (string memory) {
        return "UniswapV2";
    }
    
    function supportsPair(address tokenIn, address tokenOut) external view override returns (bool) {
        // Simplified check - in production, would query pair existence
        return tokenIn != address(0) && tokenOut != address(0) && tokenIn != tokenOut;
    }
    
    function getRouter() external view override returns (address) {
        return router;
    }
    
    function pause() external override {
        // In production, add access control
        paused = true;
    }
    
    function unpause() external override {
        // In production, add access control
        paused = false;
    }
    
    function isPaused() external view override returns (bool) {
        return paused;
    }
    
    modifier whenNotPaused() {
        if (paused) revert Paused();
        _;
    }
}

interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint amountIn,
        uint amountOutMin,
        address[] calldata path,
        address to,
        uint deadline
    ) external returns(uint[] memory amounts);
    
    function getAmountsOut(uint amountIn, address[] calldata path) external view returns (uint[] memory amounts);
    
    function factory() external view returns (address);
    
    function WETH() external view returns (address);
}
