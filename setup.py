from setuptools import find_packages, setup


setup(
    name="pigeon",
    version="0.1.0",
    description="Run commands from GPU machine through CPU worker using shared directory only.",
    packages=find_packages(),
    entry_points={"console_scripts": ["pigeon=pigeon.cli:main"]},
    python_requires=">=3.9",
)
