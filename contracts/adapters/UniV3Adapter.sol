// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./IDexAdapter.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/**
 * @title UniswapV3Adapter
 * @dev Adapter for Uniswap V3 DEX
 * @notice Implements IDexAdapter for Uniswap V3 swap operations
 */
contract UniswapV3Adapter is IDexAdapter {
    using SafeERC20 for IERC20;
    
    address public immutable router;
    address public immutable quoter;
    bool public paused;
    
    error Paused();
    error InvalidRouter();
    error InvalidPath();
    error InsufficientOutput();
    error ExpiredDeadline();
    
    event SwapExecuted(
        address indexed tokenIn,
        address indexed tokenOut,
        uint256 amountIn,
        uint256 amountOut,
        address indexed recipient
    );
    
    constructor(address _router, address _quoter) {
        if (_router == address(0)) revert InvalidRouter();
        router = _router;
        quoter = _quoter;
        paused = false;
    }
    
    function swap(bytes calldata data) external override whenNotPaused {
        (address tokenIn, address tokenOut, uint24 fee, address recipient, uint256 deadline, uint256 amountIn, uint256 amountOutMinimum, uint160 sqrtPriceLimitX96) = 
            abi.decode(data, (address, address, uint24, address, uint256, uint256, uint256, uint160));
        
        if (block.timestamp > deadline) revert ExpiredDeadline();
        
        IERC20(tokenIn).safeTransferFrom(msg.sender, address(this), amountIn);
        IERC20(tokenIn).forceApprove(router, amountIn);
        
        IUniswapV3Router.ExactInputSingleParams memory params = IUniswapV3Router.ExactInputSingleParams({
            tokenIn: tokenIn,
            tokenOut: tokenOut,
            fee: fee,
            recipient: recipient,
            deadline: deadline,
            amountIn: amountIn,
            amountOutMinimum: amountOutMinimum,
            sqrtPriceLimitX96: sqrtPriceLimitX96
        });
        
        uint256 amountOut = IUniswapV3Router(router).exactInputSingle(params);
        
        if (amountOut < amountOutMinimum) revert InsufficientOutput();
        
        emit SwapExecuted(tokenIn, tokenOut, amountIn, amountOut, recipient);
    }
    
    function getName() external pure override returns (string memory) {
        return "UniswapV3";
    }
    
    function supportsPair(address tokenIn, address tokenOut) external view override returns (bool) {
        // Simplified check - in production, would query pool existence
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

interface IUniswapV3Router {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    
    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);
    function exactInput(ExactInputParams calldata params) external payable returns (uint256 amountOut);
    
    struct ExactInputParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
}