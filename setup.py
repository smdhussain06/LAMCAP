from setuptools import setup, find_packages

setup(
    name="lamcap",
    version="0.1.0",
    py_modules=["lamcap"],
    install_requires=[
        "anthropic>=0.30.0",
        "rich>=13.0.0",
        "prompt_toolkit>=3.0.40",
        "requests>=2.31.0",
    ],
    entry_points={
        "console_scripts": [
            "lamcap=lamcap:main",
        ],
    },
    author="smdhussain06",
    description="Local Agentic Multi-Context Automation Protocol",
    python_requires=">=3.10",
)
