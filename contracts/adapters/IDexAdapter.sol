// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title IDexAdapter
 * @dev Interface for DEX adapters in arbitrage system
 * @notice Standard interface that all DEX adapters must implement
 */
interface IDexAdapter {
    /**
     * @dev Execute a swap operation
     * @param data Abi-encoded swap parameters specific to the DEX
     */
    function swap(bytes calldata data) external;
    
    /**
     * @dev Get the adapter name for identification
     */
    function getName() external view returns (string memory);
    
    /**
     * @dev Check if adapter supports a specific token pair
     * @param tokenIn Input token address
     * @param tokenOut Output token address  
     */
    function supportsPair(address tokenIn, address tokenOut) external view returns (bool);
    
    /**
     * @dev Get the router contract address used by this adapter
     */
    function getRouter() external view returns (address);
    
    /**
     * @dev Emergency pause functionality
     */
    function pause() external;
    
    /**
     * @dev Unpause functionality
     */
    function unpause() external;
    
    /**
     * @dev Check if adapter is currently paused
     */
    function isPaused() external view returns (bool);
}

/**
 * @title IUniswapV2Adapter
 * @dev Specific interface for Uniswap V2 style adapters
 */
interface IUniswapV2Adapter is IDexAdapter {
    /**
     * @dev Swap exact tokens for tokens (V2 style)
     * @param amountIn Amount of input tokens
     * @param amountOutMin Minimum amount of output tokens
     * @param path Token path for the swap
     * @param to Recipient address
     * @param deadline Transaction deadline
     */
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

/**
 * @title IUniswapV3Adapter
 * @dev Specific interface for Uniswap V3 style adapters
 */
interface IUniswapV3Adapter is IDexAdapter {
    /**
     * @dev Swap exact input single (V3 style)
     * @param params Swap parameters
     */
    function exactInputSingle(
        ExactInputSingleParams calldata params
    ) external payable returns (uint256 amountOut);
    
    /**
     * @dev Swap exact input (V3 style)  
     * @param params Swap parameters
     */
    function exactInput(
        ExactInputParams calldata params
    ) external payable returns (uint256 amountOut);
    
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